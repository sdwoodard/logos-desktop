"""Wine runner browsing and recovery controls over an immutable worker result."""

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QLabel, QListWidget, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from logos_desktop.runtime_recovery import APPLY_RETURN_PHASES, RETURN_PHASES
from .pages import heading


def paragraph(name):
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setAccessibleName(name)
    return label


class WineRunners(QWidget):
    requested = Signal(str)

    def __init__(self):
        super().__init__()
        self.view = None
        self.row = None
        self.snapshot = None
        self.setup = False
        self.retries = ()
        self.visible_rows = ()
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addWidget(heading("Wine runners"))
        self.current = paragraph("Current installation and recovery")
        layout.addWidget(self.current)
        self.family = QComboBox()
        self.family.setAccessibleName("Wine family")
        layout.addWidget(self.family)
        self.installed = QCheckBox("Installed only")
        layout.addWidget(self.installed)
        self.list = QListWidget()
        self.list.setAccessibleName("Wine versions")
        self.list.setMinimumHeight(68)
        self.list.setMaximumHeight(116)
        layout.addWidget(self.list)
        self.detail = paragraph("Runner details and availability")
        layout.addWidget(self.detail)
        self.transfer = paragraph("Transfer progress")
        layout.addWidget(self.transfer)
        self.cancel = QPushButton("Cancel download")
        self.cancel.clicked.connect(lambda: self.requested.emit("cancel"))
        layout.addWidget(self.cancel)
        self.cancel.hide()
        self.transfer.hide()
        self.recipe = QComboBox()
        self.recipe.setAccessibleName("Settings recipe")
        layout.addWidget(self.recipe)
        self.advanced = QCheckBox("Advanced evidence")
        layout.addWidget(self.advanced)
        self.evidence = QPlainTextEdit()
        self.evidence.setAccessibleName("Evidence and retained copies")
        self.evidence.setReadOnly(True)
        self.evidence.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.addWidget(self.evidence)
        self.evidence.hide()
        self.advanced.toggled.connect(self.evidence.setVisible)
        self.buttons = {}
        self.actions = QGridLayout()
        layout.addLayout(self.actions)
        for action, title in (
            ("download", "Download"), ("retry", "Retry download"), ("cancel_transfer", "Cancel active download"),
            ("draft", "Choose for setup"),
            ("try", "Try offline…"), ("apply", "Use for this installation…"),
            ("skip", "Apply without successful trial…"), ("remove", "Remove download…"),
            ("restart", "Restart trial"), ("stop", "End trial and keep current"), ("keep", "Keep current"),
            ("return", "Return to saved point…"),
            ("inspect", "Inspect restored copy…"), ("newer", "Inspect retained work…"),
            ("resume", "Resume online…"), ("feedback", "This installation works for me"),
            ("import", "Import AppImage copy…"), ("recheck", "Recheck status"), ("back", "Back"),
        ):
            button = QPushButton(title, self)
            button.hide()
            button.clicked.connect(lambda checked=False, action=action: self.requested.emit(action))
            self.buttons[action] = button
        layout.addStretch()
        self.family.currentIndexChanged.connect(self._filter)
        self.installed.toggled.connect(self._filter)
        self.list.currentRowChanged.connect(self._details)
        self.recipe.currentIndexChanged.connect(lambda: self._details(self.list.currentRow(), recipe_changed=True))
        self.advanced.toggled.connect(lambda: self._details(self.list.currentRow(), recipe_changed=True))

    def transfer_state(self, version):
        busy = bool(version)
        self.cancel.setVisible(busy)
        self.transfer.setVisible(busy)
        for widget in (self.list, self.installed, self.recipe, self.family, self.advanced):
            widget.setEnabled(not busy)
        if busy:
            self.transfer.setText(f"Preparing {version} download")
            for button in self.buttons.values():
                button.setEnabled(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange()

    def _arrange(self):
        columns = 2 if self.width() >= 640 else 1
        index = 0
        for button in self.buttons.values():
            if not button.isHidden():
                self.actions.addWidget(button, index // columns, index % columns)
                index += 1

    def load(self, snapshot, view, retries):
        self.snapshot, self.view, self.retries = snapshot, view, retries
        old = self.family.currentData()
        self.family.blockSignals(True)
        self.family.clear()
        self.family.addItem("All Wine families", "")
        for family in dict.fromkeys(row.family for row in view.rows):
            self.family.addItem(family.replace("-", " ").title(), family)
        self.family.setCurrentIndex(max(0, self.family.findData(old)))
        self.family.blockSignals(False)
        current = next((row.version for row in view.rows if row.current), snapshot.runtime)
        retained = ""
        if view.retained:
            allocated = sum(copy.allocated or 0 for copy in view.retained)
            unknown = sum(copy.allocated is None for copy in view.retained)
            retained = (f"{len(view.retained)} retained copies. Measured allocations: {allocated:,} bytes. "
                        "Shared extents can use less disk. See Advanced evidence for each copy.")
            if unknown:
                retained += f" {unknown} sizes are unknown."
        self.current.setText(f"Current Wine: {current}\nLogos target: {view.target}\n"
                             + "\n".join(filter(None, (view.space, retained, view.recovery.description))))
        self._filter()

    def _filter(self):
        if not self.view:
            return
        family = self.family.currentData()
        rows = [row for row in self.view.rows if not family or row.family == family]
        self.installed.setText(f"Installed only ({sum(bool(row.path) for row in rows)} of {len(rows)} in catalog)")
        old = self.row.runner if self.row else None
        self.visible_rows = tuple(row for row in rows if not self.installed.isChecked() or row.path)
        self.list.blockSignals(True)
        self.list.clear()
        for row in self.visible_rows:
            badges = [row.version, "Downloaded" if row.path else "Available"]
            if row.current:
                badges.append("Current")
            if row.blockers:
                badges.append("Protected")
            if row.result:
                badges.append(row.transfer if row.result == "pending" else row.result.capitalize())
            self.list.addItem(" · ".join(badges))
        index = next((i for i, row in enumerate(self.visible_rows) if row.runner == old), 0)
        self.list.setCurrentRow(index)
        self.list.blockSignals(False)
        self._details(self.list.currentRow())

    def _details(self, index, *, recipe_changed=False):
        if self.view is None:
            return
        row = self.visible_rows[index] if index >= 0 else None
        if not recipe_changed:
            previous = self.recipe.currentData()
            self.recipe.blockSignals(True)
            self.recipe.clear()
            for choice in row.choices if row else ():
                self.recipe.addItem(choice.label, choice.stack)
            if self.setup and row and row.stable_fallback:
                self.recipe.addItem("Stable runner without a project recipe", "")
            self.recipe.setCurrentIndex(max(0, self.recipe.findData(previous)))
            self.recipe.blockSignals(False)
        choice = next((c for c in row.choices if c.stack == self.recipe.currentData()), None) if row else None
        self.recipe.setVisible(self.recipe.count() > 1)
        if choice:
            row = replace(row, stack=choice.stack, affected=row.affected or choice.evidence == "known-affected",
                          setup_allowed=choice.selectable and choice.evidence == "tested",
                          evidence=choice.evidence.replace("-", " ").capitalize())
        elif row and self.setup:
            row = replace(row, stack="", setup_allowed=row.stable_fallback, evidence="No project recipe selected")
        self.row = row
        recovery = self.view.recovery
        state = self.snapshot
        trial = bool(state.trial_operation)
        running = any(value in ("Starting", "Running", "Stopping") for value in (state.state, state.indexing))
        text = []
        available = {"recheck", "back", "import"}
        disabled = set()
        if row:
            text.append(f"{row.version} · {row.size:,} bytes. {row.evidence}.")
            if row.trial_space:
                text.append(row.trial_space)
            recipe = choice.details if choice else "No matching recipe for this build."
            self.evidence.setPlainText(f"Source: {row.source}\nArtifact SHA-256: {row.digest}\n"
                                  f"Project evidence: {row.evidence}.\n{recipe}\n"
                                  f"Local trial status: {self.view.trial_reason}.\n"
                                  "User observations: check Library, search, typing, scrolling and restart. "
                                  "These observations describe only your installation.")
            if row.path:
                available.add("remove")
                if row.blockers or row.current:
                    disabled.add("remove")
                    names = {"profile-selection": "selected by an installation profile",
                             "return-point": "kept for recovery", "retained-generation": "retained newer work",
                             "trial": "reserved for a trial", "helper": "offline helper still owns this version"}
                    reasons = (names.get(r, r.replace("-", " ")) for r in row.blockers or ("current selection",))
                    text.append("Removal blocked: " + ", ".join(reasons) + ".")
            else:
                if row.result == "pending":
                    available.add("cancel_transfer")
                    text.append(f"Transfer in progress: {row.transfer}.")
                elif row.result == "unknown":
                    text.append("Publication status unknown. Preserve the download and recheck before retrying.")
                else:
                    available.add("retry" if row.runner in self.retries or row.operation else "download")
                if row.reason:
                    text.append("Transfer status: " + row.reason.replace("-", " ") + ".")
                text.append("Download prepares verified bytes. It keeps your current selection.")
            if self.setup:
                available.add("draft")
                if not row.path or not row.setup_allowed or row.affected:
                    disabled.add("draft")
                    text.append("Choose a downloaded candidate matching the latest stable Logos target.")
            else:
                choices = {"try", "apply", "skip"}
                available.update(choices)
                reason = ("Download the runner first." if not row.path else
                          "No eligible stack matches this installation." if not row.stack or row.affected else
                          "Finish setup before trying or applying Wine." if not state.installed else
                          "End the trial to release the source." if trial else
                          "Close Logos and its indexer first." if running else self.view.prerequisite)
                if reason:
                    text.append(reason)
                    disabled.update(choices)
                if row.trial_error:
                    text.append(row.trial_error)
                    disabled.add("try")
                if row.apply_error:
                    text.append(row.apply_error)
                    disabled.update(("apply", "skip"))
                if (row.generation, row.stack) != self.view.trial_choice:
                    disabled.add("apply")
                    text.append("No matching successful trial. Try offline first or explicitly skip the trial.")
                if not self.view.current_owned:
                    disabled.update(("apply", "skip"))
                    text.append("Apply needs a managed current Wine download for recovery. Keep native or custom Wine.")
                if recovery.blocked and recovery.phase not in ("preparing", "point-verified"):
                    disabled.update(choices)
                    text.append("Complete the pending recovery before changing Wine.")
                if row.current:
                    disabled.update(("apply", "skip"))
                    text.append("This version is already selected.")
                if choice and choice.experimental:
                    text.append("Experimental candidate. Open Advanced evidence to review before trying or applying.")
                    if not self.advanced.isChecked():
                        disabled.update(choices)
                if choice and not choice.selectable:
                    if not choice.experimental or not self.advanced.isChecked():
                        disabled.update(("apply", "skip"))
                        text.append("Apply unavailable: " + choice.details.splitlines()[0] + ".")
                text.append("Try opens an offline temporary copy. Keep current leaves the original in place. "
                            "Applying always requires a verified recovery point and an offline check of the old copy.")
        else:
            text.append("No versions match this filter.")
            self.evidence.clear()
        if self.view.retained:
            sizes = [f"{c.purpose}: {c.location}\n"
                     + (f"{c.logical:,} logical bytes, {c.allocated:,} allocated bytes."
                        if c.allocated is not None else "Size unknown. Preserve and recheck.")
                     for c in self.view.retained]
            self.evidence.appendPlainText("\nRetained copies\n" + "\n".join(sizes))
        if trial:
            available.update(("restart", "stop"))
        if recovery.phase in ("preparing", "point-verified"):
            available.add("keep")
        if recovery.phase in (*APPLY_RETURN_PHASES, *RETURN_PHASES):
            available.add("return")
        if recovery.phase in ("returned", "resume-intent"):
            available.update(("inspect", "newer", "resume"))
            if not recovery.inspected or self.view.prerequisite:
                disabled.add("resume")
                text.append(self.view.prerequisite or "Inspect the restored installation offline before online resume.")
        if recovery.phase == "applied":
            available.add("feedback")
        self.detail.setText("\n".join(text))
        for key, button in self.buttons.items():
            button.setVisible(key in available)
            button.setEnabled(key not in disabled)
        self._arrange()
