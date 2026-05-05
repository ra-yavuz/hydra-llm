PREFIX ?= /usr
SYSCONFDIR ?= /etc
BINDIR = $(DESTDIR)$(PREFIX)/bin
LIBDIR = $(DESTDIR)$(PREFIX)/lib/hydra-llm
SHAREDIR = $(DESTDIR)$(PREFIX)/share/hydra-llm
DOCDIR = $(DESTDIR)$(PREFIX)/share/doc/hydra-llm
MANDIR = $(DESTDIR)$(PREFIX)/share/man/man1
COMPLETIONDIR = $(DESTDIR)$(PREFIX)/share/bash-completion/completions

# For dev-install only; uses the user's home, no sudo needed.
USER_BIN = $(HOME)/.local/bin
USER_LIB = $(HOME)/.local/share/hydra-llm/lib
USER_SHARE = $(HOME)/.local/share/hydra-llm/share

.PHONY: all install uninstall dev-install dev-uninstall user-install user-uninstall lint check deb clean

all:
	@echo "Targets:"
	@echo "  make user-install    end-user install into ~/.local (copies files, no sudo)"
	@echo "  make user-uninstall  remove user-install (keeps user data in ~/.config)"
	@echo "  make dev-install     dev install: symlinks source tree into ~/.local"
	@echo "  make dev-uninstall   undo dev-install"
	@echo "  make install         system-wide install (used by the deb postinst)"
	@echo "  make uninstall       remove system-wide install"
	@echo "  make deb             build .deb packages"
	@echo "  make lint            shellcheck + python -m py_compile"

install:
	install -d $(BINDIR) $(LIBDIR)/hydra_llm $(SHAREDIR) $(SHAREDIR)/presets \
	           $(SHAREDIR)/personas $(SHAREDIR)/docker $(SHAREDIR)/scripts \
	           $(DOCDIR) $(COMPLETIONDIR)
	install -m 0755 bin/hydra-llm                 $(BINDIR)/hydra-llm
	install -m 0644 lib/hydra_llm/*.py            $(LIBDIR)/hydra_llm/
	install -m 0644 catalog/catalog.yaml          $(SHAREDIR)/catalog.yaml
	install -m 0644 personas/friendly-tutor.md    $(SHAREDIR)/personas/friendly-tutor.md
	install -m 0644 personas/concise-coder.md     $(SHAREDIR)/personas/concise-coder.md
	install -m 0644 docker/Dockerfile.vulkan      $(SHAREDIR)/docker/Dockerfile.vulkan
	install -m 0644 docker/Dockerfile.cpu         $(SHAREDIR)/docker/Dockerfile.cpu
	install -m 0755 scripts/user-uninstall.sh     $(SHAREDIR)/scripts/user-uninstall.sh
	install -m 0644 README.md                     $(DOCDIR)/README.md
	install -m 0644 LICENSE                       $(DOCDIR)/LICENSE

uninstall:
	rm -f $(BINDIR)/hydra-llm
	rm -rf $(LIBDIR)
	rm -rf $(SHAREDIR)
	rm -rf $(DOCDIR)

# Dev install: symlinks from the working tree into ~/.local, so edits to source
# are picked up immediately. No sudo. Mirrors the same FHS layout we'd use for
# the system install, just rooted at ~/.local/share/hydra-llm.
dev-install:
	@bash scripts/dev-install.sh "$(USER_BIN)" "$(USER_LIB)" "$(USER_SHARE)"

dev-uninstall:
	rm -f $(USER_BIN)/hydra-llm
	rm -rf $(USER_LIB) $(USER_SHARE)
	@echo "Removed dev install (user config in ~/.config/hydra-llm is kept)."

# End-user install: copies files (no symlinks) into ~/.local, so the user
# can delete the source tree afterwards. This is what get.sh runs.
user-install:
	@bash scripts/user-install.sh "$(USER_BIN)" "$(USER_LIB)" "$(USER_SHARE)"

user-uninstall:
	@bash scripts/user-uninstall.sh "$(USER_BIN)" "$(USER_LIB)" "$(USER_SHARE)" keep-data


lint:
	shellcheck bin/hydra-llm scripts/*.sh || true
	python3 -m py_compile lib/hydra_llm/*.py

check: lint

deb:
	bash scripts/build-deb.sh

clean:
	rm -rf dist build *.deb
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
