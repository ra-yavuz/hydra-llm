import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PC3
import org.kde.plasma.extras as PE
import org.kde.plasma.plasma5support as P5Support
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    // ---- State ----
    property var lastStatus: ({ ok: false, models: [], extra_running: [], summary: {} })
    property int runningCount: 0
    property int startingCount: 0
    property real cpuPct: 0
    property real ramPct: 0
    property real gpuPct: 0
    property real vramPct: 0
    property string lastError: ""
    // The hydra-llm CLI is on the user's PATH after install.
    property string cliPath: "hydra-llm"
    // Tracks aliases the user just hit Start on, until they show up healthy.
    // Each value is an epoch-ms timestamp; entries auto-expire after pendingStartTimeoutMs
    // so a crashed container doesn't leave the icon spinning forever.
    property var pendingStarts: ({})
    readonly property int pendingStartTimeoutMs: 180000  // 3 minutes

    // Logs viewer state.
    property string logAlias: ""
    property string logBuffer: ""
    property bool logVisible: false

    // Editor state. editAlias=="" means the editor panel is hidden.
    property string editAlias: ""
    property string promptLoaded: ""        // last value fetched from disk
    property string promptSource: "none"
    property int    promptLoadToken: 0      // bump to force the TextArea to re-bind
    property var    paramsLoaded: ({})      // {key: value} as fetched
    property var    paramsCurrent: ({})     // user-edited values
    property var    paramsOverrides: ({})   // {key: 'inline'|'file'}

    // ---- Tray look ----
    toolTipMainText: runningCount > 0
        ? i18np("%1 model running", "%1 models running", runningCount)
        : i18n("No models running")
    toolTipSubText: buildTooltip()

    function buildTooltip() {
        if (lastError) return i18n("Error: %1", lastError)
        var lines = []
        for (var i = 0; i < lastStatus.models.length; i++) {
            var m = lastStatus.models[i]
            if (m.running) {
                var tag = m.ready ? "" : " [starting]"
                lines.push(m.alias + "  :" + m.running_port + tag)
            }
        }
        var s = lastStatus.summary || {}
        if (Object.keys(s).length) {
            lines.push("CPU "  + (s.cpu_pct  || 0) + "%   RAM "  + (s.ram_pct  || 0) + "% (" + (s.ram_used_mb || 0)  + "/" + (s.ram_total_mb || 0)  + " MiB)")
            if ((s.gpu_pct || 0) > 0 || (s.vram_total_mb || 0) > 0) {
                lines.push("GPU "  + (s.gpu_pct  || 0) + "%   VRAM " + (s.vram_pct || 0) + "% (" + (s.vram_used_mb || 0) + "/" + (s.vram_total_mb || 0) + " MiB)")
            }
        }
        return lines.length ? lines.join("\n") : i18n("Click to start a model")
    }

    // ---- Backend invocation ----
    P5Support.DataSource {
        id: runner
        engine: "executable"
        connectedSources: []
        property var pending: ({})

        onNewData: (sourceName, data) => {
            var stdout = (data["stdout"] || "").trim()
            var stderr = (data["stderr"] || "").trim()
            var tag = pending[sourceName] || "status"
            delete pending[sourceName]
            disconnectSource(sourceName)
            if (tag === "status") handleStatus(stdout, stderr)
            else if (tag === "action") handleAction(stdout, stderr)
            else if (tag === "logs") handleLogs(stdout, stderr)
            else if (tag === "prompt-get") handlePromptGet(stdout, stderr)
            else if (tag === "prompt-write") handlePromptWrite(stdout, stderr)
            else if (tag === "params-get") handleParamsGet(stdout, stderr)
            else if (tag === "params-write") handleParamsWrite(stdout, stderr)
            // 'fire' tag: nothing to do.
        }

        function exec(cmd, tag) {
            pending[cmd] = tag
            connectSource(cmd)
        }
    }

    function shellEscape(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'" }

    function refresh() {
        runner.exec(shellEscape(cliPath) + " tray status", "status")
        // Poll logs while a model is starting (so the buffer is fresh when the
        // pane is shown) OR while the pane is open.
        var fetchAlias = logAlias
        if (!fetchAlias) {
            for (var a in pendingStarts) { fetchAlias = a; break }
        }
        if (fetchAlias) {
            runner.exec(shellEscape(cliPath) + " tray logs " + shellEscape(fetchAlias) + " --tail 80", "logs")
            if (!logAlias) logAlias = fetchAlias
        }
        // Expire stale pendingStarts.
        var now = Date.now()
        var changed = false
        for (var pa in pendingStarts) {
            if (now - pendingStarts[pa] > pendingStartTimeoutMs) {
                delete pendingStarts[pa]
                changed = true
            }
        }
        if (changed) pendingStarts = Object.assign({}, pendingStarts)
    }

    function startModel(alias) {
        runner.exec(shellEscape(cliPath) + " start " + shellEscape(alias) + " --json", "action")
        pendingStarts[alias] = Date.now()
        pendingStarts = Object.assign({}, pendingStarts)
        // Auto-open the log pane so the user sees the model loading.
        showLogs(alias)
    }
    function stopModel(alias) { runner.exec(shellEscape(cliPath) + " stop " + shellEscape(alias), "action") }
    function stopAll()        { runner.exec(shellEscape(cliPath) + " stop-all", "action") }
    function downloadModel(alias) {
        // Spawn a terminal so the user sees the progress bar (downloads are big).
        runner.exec(shellEscape(cliPath) + " tray chat-spawn " + shellEscape(alias), "fire")
        // chat-spawn opens a terminal running `hydra-llm chat`, which auto-downloads
        // if the model is missing. Simpler than a separate download-spawn helper.
    }
    function chatModel(alias)  { runner.exec(shellEscape(cliPath) + " tray chat-spawn " + shellEscape(alias), "fire") }
    function showLogs(alias) {
        logAlias = alias
        logVisible = true
        runner.exec(shellEscape(cliPath) + " tray logs " + shellEscape(alias) + " --tail 80", "logs")
    }
    function hideLogs() { logVisible = false; logAlias = "" }

    // ---- Editor ----
    function openEditor(alias) {
        if (editAlias === alias) { editAlias = ""; return }
        editAlias = alias
        runner.exec(shellEscape(cliPath) + " tray get-prompt " + shellEscape(alias), "prompt-get")
        runner.exec(shellEscape(cliPath) + " tray get-params " + shellEscape(alias), "params-get")
    }
    function closeEditor() { editAlias = "" }

    function savePrompt(content) {
        if (!editAlias) return
        // Base64-encoded stdin for safe transport of multi-line UTF-8.
        var b64 = Qt.btoa(content)
        var cmd = "printf 'b64:%s' " + shellEscape(b64) + " | "
                + shellEscape(cliPath) + " tray set-prompt " + shellEscape(editAlias)
        runner.exec(cmd, "prompt-write")
    }
    function clearPrompt() {
        if (!editAlias) return
        runner.exec(shellEscape(cliPath) + " tray clear-prompt " + shellEscape(editAlias), "prompt-write")
    }
    function saveParams() {
        if (!editAlias) return
        var json = JSON.stringify(paramsCurrent)
        var b64 = Qt.btoa(json)
        var cmd = "printf 'b64:%s' " + shellEscape(b64) + " | "
                + shellEscape(cliPath) + " tray set-params " + shellEscape(editAlias)
        runner.exec(cmd, "params-write")
    }
    function clearParams() {
        if (!editAlias) return
        runner.exec(shellEscape(cliPath) + " tray clear-params " + shellEscape(editAlias), "params-write")
    }
    function revertParams() {
        paramsCurrent = Object.assign({}, paramsLoaded)
    }

    function handlePromptGet(out, err) {
        if (!out) return
        try {
            var d = JSON.parse(out)
            if (d.ok) {
                promptLoaded = d.content || ""
                promptSource = d.source
                promptLoadToken += 1
            }
        } catch (e) { /* ignore */ }
    }
    function handlePromptWrite(out, err) {
        if (!editAlias) return
        // Re-fetch so editor shows what's actually on disk.
        runner.exec(shellEscape(cliPath) + " tray get-prompt " + shellEscape(editAlias), "prompt-get")
    }
    function handleParamsGet(out, err) {
        if (!out) return
        try {
            var d = JSON.parse(out)
            if (d.ok) {
                paramsLoaded = d.params || {}
                paramsCurrent = Object.assign({}, paramsLoaded)
                paramsOverrides = d.overrides || {}
            }
        } catch (e) { /* ignore */ }
    }
    function handleParamsWrite(out, err) {
        if (!editAlias) return
        runner.exec(shellEscape(cliPath) + " tray get-params " + shellEscape(editAlias), "params-get")
    }
    function setParamValue(key, value) {
        var c = Object.assign({}, paramsCurrent)
        c[key] = value
        paramsCurrent = c
    }

    function handleStatus(out, err) {
        if (!out) { lastError = err || "no output from hydra-llm tray status"; return }
        try {
            var parsed = JSON.parse(out)
            if (!parsed.ok) { lastError = parsed.error || "unknown error"; return }
            lastError = ""
            lastStatus = parsed
            var ready = 0, starting = 0
            for (var i = 0; i < parsed.models.length; i++) {
                var m = parsed.models[i]
                if (m.running && m.ready) ready++
                else if (m.running && !m.ready) starting++
                if (m.ready && pendingStarts[m.alias]) {
                    delete pendingStarts[m.alias]
                }
            }
            ready += (parsed.extra_running || []).length
            for (var alias in pendingStarts) starting++
            runningCount = ready
            startingCount = starting
            var s = parsed.summary || {}
            cpuPct = (s.cpu_pct || 0) / 100.0
            ramPct = (s.ram_pct || 0) / 100.0
            gpuPct = (s.gpu_pct || 0) / 100.0
            vramPct = (s.vram_pct || 0) / 100.0
        } catch (e) {
            lastError = "bad json: " + e
        }
    }

    function handleAction(out, err) {
        if (err) lastError = err
        Qt.callLater(refresh)
    }

    function handleLogs(out, err) {
        if (!out) return
        try {
            var d = JSON.parse(out)
            if (d.ok && d.lines) {
                logBuffer = d.lines.join("\n")
            } else if (d.missing) {
                // Container is gone (crashed or removed). Close the panel and clear pending.
                logVisible = false
                if (pendingStarts[logAlias]) {
                    delete pendingStarts[logAlias]
                    pendingStarts = Object.assign({}, pendingStarts)
                }
                logAlias = ""
            }
        } catch (e) { /* leave buffer alone */ }
    }

    Component.onCompleted: refresh()

    Timer {
        interval: 2000
        running: true
        repeat: true
        onTriggered: refresh()
    }

    // ---- Compact representation: HAL 9000 eye ----
    compactRepresentation: MouseArea {
        id: hal
        anchors.fill: parent
        hoverEnabled: true
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        onClicked: (mouse) => {
            if (mouse.button === Qt.RightButton) contextMenu.popup()
            else root.expanded = !root.expanded
        }

        readonly property real activity: Math.max(root.cpuPct, root.gpuPct, root.ramPct, root.vramPct)
        readonly property bool alive: root.runningCount > 0 || root.startingCount > 0
        readonly property bool loading: root.startingCount > 0
        readonly property bool errored: root.lastError.length > 0

        Item {
            id: eye
            width: Math.min(parent.width, parent.height)
            height: width
            anchors.centerIn: parent

            Rectangle {                                   // bezel
                anchors.fill: parent; radius: width / 2
                color: "#0a0a0a"; border.color: "#1c1c1c"; border.width: Math.max(1, width * 0.04)
            }

            Rectangle {                                   // glow (breathes)
                id: glow
                width: parent.width * 0.92; height: width; radius: width / 2
                anchors.centerIn: parent
                gradient: Gradient {
                    GradientStop { position: 0.0; color: "#ff2a2a" }
                    GradientStop { position: 0.55; color: "#7a0000" }
                    GradientStop { position: 1.0; color: "transparent" }
                }
                opacity: (hal.alive ? 0.85 : 0.35) * (0.7 + 0.3 * hal.activity)
                SequentialAnimation on scale {
                    running: true; loops: Animation.Infinite
                    NumberAnimation { from: 0.92; to: 1.05; duration: Math.round(1800 - hal.activity * 1100); easing.type: Easing.InOutSine }
                    NumberAnimation { from: 1.05; to: 0.92; duration: Math.round(1800 - hal.activity * 1100); easing.type: Easing.InOutSine }
                }
            }

            Rectangle {                                   // iris (lens)
                id: iris
                width: parent.width * 0.55; height: width; radius: width / 2
                anchors.centerIn: parent
                gradient: Gradient {
                    GradientStop { position: 0.0;  color: "#ffe4d6" }
                    GradientStop { position: 0.18; color: "#ffb060" }
                    GradientStop { position: 0.45; color: "#ff2020" }
                    GradientStop { position: 1.0;  color: "#660000" }
                }
                opacity: hal.alive ? 1.0 : 0.55
                SequentialAnimation on opacity {
                    running: hal.errored
                    loops: Animation.Infinite
                    NumberAnimation { to: 0.4; duration: 80 }
                    NumberAnimation { to: 1.0; duration: 60 }
                    PauseAnimation { duration: 800 + Math.random() * 1200 }
                }
            }

            Rectangle {                                   // specular highlight
                width: iris.width * 0.30; height: iris.height * 0.20; radius: width / 2
                color: "#ffffff"
                opacity: 0.35 * (hal.alive ? 1.0 : 0.6)
                anchors.horizontalCenter: iris.horizontalCenter
                anchors.horizontalCenterOffset: -iris.width * 0.18
                anchors.verticalCenter: iris.verticalCenter
                anchors.verticalCenterOffset: -iris.height * 0.22
            }

            Canvas {                                      // scanning arc when loading
                id: scanRing
                anchors.fill: parent
                visible: hal.loading
                opacity: 0.85
                rotation: 0
                onPaint: {
                    var ctx = getContext("2d"); ctx.reset()
                    var cx = width/2, cy = height/2, r = width/2 - Math.max(2, width*0.06)
                    ctx.lineWidth = Math.max(2, width*0.07)
                    ctx.strokeStyle = "#ffd454"
                    ctx.shadowColor = "#ffd454"; ctx.shadowBlur = 6
                    ctx.beginPath(); ctx.arc(cx, cy, r, -Math.PI/4, Math.PI/4); ctx.stroke()
                }
                Component.onCompleted: requestPaint()
                onWidthChanged: requestPaint()
                RotationAnimator on rotation {
                    running: scanRing.visible
                    from: 0; to: 360; duration: 1400; loops: Animation.Infinite
                }
            }
        }
    }

    PC3.Menu {
        id: contextMenu
        PC3.MenuItem { text: i18n("Refresh"); onTriggered: root.refresh() }
        PC3.MenuItem { text: i18n("Stop all"); onTriggered: root.stopAll() }
    }

    // ---- Full representation (popup) ----
    fullRepresentation: ColumnLayout {
        Layout.preferredWidth: Kirigami.Units.gridUnit * 28
        Layout.preferredHeight: Kirigami.Units.gridUnit * 36
        spacing: Kirigami.Units.smallSpacing

        PE.Heading { level: 3; text: i18n("Hydra LLM"); Layout.fillWidth: true }

        Label {
            text: root.lastError ? i18n("Error: %1", root.lastError) : i18n("Config: %1", root.lastStatus.config_path || "(not loaded)")
            color: root.lastError ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.disabledTextColor
            font.pointSize: Kirigami.Theme.smallFont.pointSize
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }

        // Resource summary row.
        GridLayout {
            Layout.fillWidth: true
            columns: 2; rowSpacing: 2; columnSpacing: Kirigami.Units.smallSpacing
            Repeater {
                model: [
                    { name: "CPU",  color: "#3ec46d", pct: Math.round(root.cpuPct  * 100) },
                    { name: "RAM",  color: "#3b82f6", pct: Math.round(root.ramPct  * 100) },
                    { name: "GPU",  color: "#f59e0b", pct: Math.round(root.gpuPct  * 100) },
                    { name: "VRAM", color: "#a855f7", pct: Math.round(root.vramPct * 100) },
                ]
                delegate: RowLayout {
                    Layout.fillWidth: true; spacing: Kirigami.Units.smallSpacing
                    Rectangle {
                        width: 8; height: 8; radius: 4
                        color: modelData.color
                        opacity: 0.25 + 0.75 * Math.min(1.0, modelData.pct / 100)
                    }
                    Label {
                        text: modelData.name + " " + modelData.pct + "%"
                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                        Layout.fillWidth: true; elide: Text.ElideRight
                    }
                }
            }
        }

        Rectangle { Layout.fillWidth: true; height: 1; color: Kirigami.Theme.disabledTextColor; opacity: 0.4 }

        // Model list.
        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            ListView {
                id: list
                spacing: Kirigami.Units.smallSpacing
                model: root.lastStatus.models || []

                delegate: RowLayout {
                    width: list.width
                    spacing: Kirigami.Units.smallSpacing

                    Rectangle {
                        width: 8; height: 8; radius: 4
                        color: modelData.running
                            ? (modelData.ready ? Kirigami.Theme.positiveTextColor : Kirigami.Theme.neutralTextColor)
                            : Kirigami.Theme.disabledTextColor
                    }

                    ColumnLayout {
                        Layout.fillWidth: true; spacing: 0
                        Label {
                            text: modelData.alias + (modelData.running_port ? "   :" + modelData.running_port : "")
                            font.bold: modelData.running
                        }
                        Label {
                            text: (modelData.size_gb ? modelData.size_gb + " GB  " : "")
                                + (modelData.fit ? "[" + modelData.fit + "] " : "")
                                + (modelData.name || "")
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                            color: Kirigami.Theme.disabledTextColor
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                    }

                    PC3.ToolButton {
                        text: ""
                        icon.name: "utilities-terminal"
                        ToolTip.visible: hovered
                        ToolTip.text: i18n("Open chat in a terminal (downloads first if needed)")
                        onClicked: root.chatModel(modelData.alias)
                    }
                    PC3.ToolButton {
                        text: ""
                        icon.name: "view-list-text"
                        visible: modelData.running
                        ToolTip.visible: hovered
                        ToolTip.text: i18n("Show container logs")
                        onClicked: root.showLogs(modelData.alias)
                    }
                    PC3.ToolButton {
                        text: ""
                        icon.name: "configure"
                        ToolTip.visible: hovered
                        ToolTip.text: i18n("Edit system prompt and sampling params")
                        onClicked: root.openEditor(modelData.alias)
                    }
                    PC3.Button {
                        text: modelData.running ? i18n("Stop") : i18n("Start")
                        onClicked: modelData.running ? root.stopModel(modelData.alias) : root.startModel(modelData.alias)
                    }
                }
            }
        }

        // Inline log panel (toggled by the eye icon on each row).
        ColumnLayout {
            Layout.fillWidth: true
            visible: root.logVisible
            spacing: 2
            RowLayout {
                Layout.fillWidth: true
                Label { text: i18n("Logs: %1", root.logAlias); font.bold: true; Layout.fillWidth: true }
                PC3.ToolButton { text: i18n("Close"); onClicked: root.hideLogs() }
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.preferredHeight: Kirigami.Units.gridUnit * 8
                clip: true
                TextArea {
                    readOnly: true
                    font.family: "monospace"
                    font.pointSize: Kirigami.Theme.smallFont.pointSize - 1
                    text: root.logBuffer
                    wrapMode: Text.WrapAnywhere
                    background: Rectangle { color: Kirigami.Theme.alternateBackgroundColor }
                }
            }
        }

        // Editor panel (system prompt + sampling params).
        ColumnLayout {
            Layout.fillWidth: true
            visible: root.editAlias.length > 0
            spacing: Kirigami.Units.smallSpacing

            RowLayout {
                Layout.fillWidth: true
                Label {
                    text: i18n("Edit %1   (prompt source: %2)", root.editAlias, root.promptSource)
                    font.bold: true
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                }
                PC3.ToolButton { text: i18n("Close"); onClicked: root.closeEditor() }
            }

            // System prompt
            Label {
                text: i18n("System prompt")
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                color: Kirigami.Theme.disabledTextColor
                Layout.fillWidth: true
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.preferredHeight: Kirigami.Units.gridUnit * 6
                clip: true
                TextArea {
                    id: promptArea
                    wrapMode: Text.Wrap
                    font.family: "monospace"
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    placeholderText: i18n("(no system prompt)")
                    // Re-bind whenever a fresh load lands.
                    property int boundToken: -1
                    Connections {
                        target: root
                        function onPromptLoadTokenChanged() {
                            if (root.promptLoadToken !== promptArea.boundToken) {
                                promptArea.text = root.promptLoaded
                                promptArea.boundToken = root.promptLoadToken
                            }
                        }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing
                PC3.Button {
                    text: i18n("Save prompt")
                    enabled: root.promptSource !== "inline"
                    onClicked: root.savePrompt(promptArea.text)
                }
                PC3.Button {
                    text: i18n("Revert")
                    onClicked: { promptArea.text = root.promptLoaded; promptArea.boundToken = root.promptLoadToken }
                }
                PC3.Button {
                    text: i18n("Clear")
                    enabled: root.promptSource === "file"
                    onClicked: root.clearPrompt()
                }
                Item { Layout.fillWidth: true }
            }

            // Params grid
            Label {
                text: i18n("Sampling params")
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                color: Kirigami.Theme.disabledTextColor
                Layout.fillWidth: true
            }
            GridLayout {
                Layout.fillWidth: true
                columns: 4
                rowSpacing: 4; columnSpacing: Kirigami.Units.smallSpacing

                Repeater {
                    model: ["temperature", "top_p", "top_k", "repeat_penalty", "max_tokens", "seed"]
                    delegate: RowLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing
                        Label {
                            text: modelData
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 6
                        }
                        TextField {
                            text: root.paramsCurrent[modelData] !== undefined ? String(root.paramsCurrent[modelData]) : ""
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 4
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                            onEditingFinished: {
                                var raw = text
                                var asNum = Number(raw)
                                if (!isNaN(asNum) && raw.length > 0) root.setParamValue(modelData, asNum)
                            }
                            background: Rectangle {
                                color: root.paramsOverrides[modelData] === "inline"
                                    ? Kirigami.Theme.alternateBackgroundColor
                                    : Kirigami.Theme.backgroundColor
                                border.color: Kirigami.Theme.disabledTextColor
                                border.width: 1
                                radius: 3
                            }
                        }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing
                PC3.Button { text: i18n("Save params"); onClicked: root.saveParams() }
                PC3.Button { text: i18n("Revert"); onClicked: root.revertParams() }
                PC3.Button { text: i18n("Clear file"); onClicked: root.clearParams() }
                Item { Layout.fillWidth: true }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            PC3.Button { text: i18n("Refresh"); onClicked: root.refresh(); Layout.fillWidth: true }
            PC3.Button {
                text: i18n("Stop all")
                enabled: root.runningCount > 0
                onClicked: root.stopAll(); Layout.fillWidth: true
            }
        }
    }
}
