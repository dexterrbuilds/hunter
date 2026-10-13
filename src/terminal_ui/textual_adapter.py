"""Textual renderer isolated behind Hunter's terminal-driver port."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Label, ListItem, ListView, Static

from terminal_ui.models import (
    OperatorAction,
    OperatorConfirmation,
    TerminalSnapshot,
    TerminalView,
)
from terminal_ui.views import navigation_labels, render_view
from utils.redaction import sanitize_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from terminal_ui.ports import (
        ActionRequester,
        ConfirmationRequester,
        ExitRequester,
        PageRequester,
        RefreshRequester,
    )


class ConfirmationModal(ModalScreen[bool]):
    """Deliberate modal confirmation for one staged runtime control."""

    CSS = """
    ConfirmationModal { align: center middle; }
    #confirmation-dialog {
        width: 70; height: auto; max-height: 80%;
        border: heavy $warning; background: $surface; padding: 1 2;
    }
    #confirmation-buttons { height: 3; align: center middle; }
    #confirmation-buttons Button { margin: 0 1; }
    """

    def __init__(self, confirmation: OperatorConfirmation) -> None:
        super().__init__()
        self.confirmation = confirmation

    def compose(self) -> ComposeResult:
        with Vertical(id="confirmation-dialog"):
            yield Label("Confirm consequential action")
            yield Static(self.confirmation.summary, markup=False)
            with Horizontal(id="confirmation-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Confirm", variant="error", id="confirm")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class HunterTerminalApp(App[None]):
    """Responsive local operator dashboard with read-only trading inspection."""

    TITLE = "Hunter — Terminal Operator"
    SUB_TITLE = "local read/control adapter"
    CSS = """
    Screen { layout: vertical; }
    #workspace { height: 1fr; }
    #navigation {
        width: 28; height: 1fr; border-right: solid $primary;
        background: $panel;
    }
    #content { width: 1fr; height: 1fr; padding: 1 2; overflow: auto; }
    #status-line { height: 1; background: $boost; padding: 0 1; }
    ListItem { padding: 0 1; }
    ListItem.-highlight { background: $accent 30%; }
    """
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("r", "refresh", "Refresh"),
        ("n", "next_page", "Next page"),
        ("p", "previous_page", "Previous page"),
        ("e", "enable_trading", "Enable"),
        ("d", "disable_trading", "Disable"),
        ("k", "kill_switch", "Kill switch"),
        ("c", "clear_kill_switch", "Clear kill"),
        ("q", "quit_ui", "Close TUI"),
    ]

    def __init__(  # noqa: PLR0913
        self,
        *,
        request_action: ActionRequester,
        confirm_action: ConfirmationRequester,
        request_refresh: RefreshRequester,
        request_exit: ExitRequester,
        request_page: PageRequester,
        mounted: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._request_action = request_action
        self._confirm_action = confirm_action
        self._request_refresh = request_refresh
        self._request_exit = request_exit
        self._request_page = request_page
        self._mounted = mounted or (lambda: None)
        self._snapshot: TerminalSnapshot | None = None
        self._view = TerminalView.OVERVIEW
        self._confirmation_tasks: set[asyncio.Task[None]] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="workspace"):
            yield ListView(
                *(
                    ListItem(Label(label), id=f"nav-{view.value}")
                    for view, label in navigation_labels()
                ),
                id="navigation",
            )
            yield Static(
                "Waiting for Hunter runtime snapshot…", id="content", markup=False
            )
        yield Static(
            "TUI starting — Hunter trading runtime remains independent",
            id="status-line",
            markup=False,
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#navigation", ListView).index = 0
        self._render_snapshot()
        self._mounted()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        identifier = event.item.id or ""
        if identifier.startswith("nav-"):
            self._view = TerminalView(identifier.removeprefix("nav-"))
            self._render_snapshot()

    def apply_snapshot(self, snapshot: TerminalSnapshot) -> None:
        """Apply immutable state already collected outside the renderer."""
        self._snapshot = snapshot
        self._render_snapshot()

    def _render_snapshot(self) -> None:
        snapshot = self._snapshot
        if snapshot is None or not self.is_running:
            return
        self.query_one("#content", Static).update(render_view(snapshot, self._view))
        state = snapshot.status.application_state.value
        age = snapshot.refreshed_at.strftime("%H:%M:%S UTC")
        self.query_one("#status-line", Static).update(
            f"{snapshot.status.active_bots[0] if snapshot.status.active_bots else 'hunter'}"
            f"  |  {state}  |  refreshed {age}  |  q closes TUI only"
        )

    def action_refresh(self) -> None:
        self._request_refresh()
        self.notify("Refresh requested")

    def action_next_page(self) -> None:
        self._page(1)

    def action_previous_page(self) -> None:
        self._page(-1)

    def _page(self, direction: int) -> None:
        try:
            self._request_page(self._view.value, direction)
        except ValueError:
            self.notify("This view has no paginated history")

    async def action_enable_trading(self) -> None:
        await self._stage(OperatorAction.ENABLE_TRADING)

    async def action_disable_trading(self) -> None:
        await self._stage(OperatorAction.DISABLE_TRADING)

    async def action_kill_switch(self) -> None:
        await self._stage(OperatorAction.ACTIVATE_KILL_SWITCH)

    async def action_clear_kill_switch(self) -> None:
        await self._stage(OperatorAction.CLEAR_KILL_SWITCH)

    def action_quit_ui(self) -> None:
        self._request_exit()
        self.exit()

    async def _stage(self, action: OperatorAction) -> None:
        try:
            confirmation = await self._request_action(action)
        except Exception as error:  # noqa: BLE001
            self.notify(sanitize_text(error), severity="error")
            return

        def complete(confirmed: bool | None) -> None:  # noqa: FBT001
            if confirmed:
                task = asyncio.create_task(self._confirm(confirmation.nonce))
                self._confirmation_tasks.add(task)
                task.add_done_callback(self._confirmation_tasks.discard)

        self.push_screen(ConfirmationModal(confirmation), complete)

    async def _confirm(self, nonce: str) -> None:
        try:
            await self._confirm_action(nonce)
        except Exception as error:  # noqa: BLE001
            self.notify(sanitize_text(error), severity="error", timeout=6)
        else:
            self.notify("Control applied after final-state revalidation")


class TextualTerminalDriver:
    """Own one Textual application without owning Hunter's process lifecycle."""

    def __init__(self) -> None:
        self._app: HunterTerminalApp | None = None
        self._pending_snapshot: TerminalSnapshot | None = None
        self._ready = asyncio.Event()

    async def start(
        self,
        *,
        request_action: ActionRequester,
        confirm_action: ConfirmationRequester,
        request_refresh: RefreshRequester,
        request_exit: ExitRequester,
        request_page: PageRequester,
    ) -> None:
        self._app = HunterTerminalApp(
            request_action=request_action,
            confirm_action=confirm_action,
            request_refresh=request_refresh,
            request_exit=request_exit,
            request_page=request_page,
            mounted=self._ready.set,
        )
        if self._pending_snapshot is not None:
            self._app.apply_snapshot(self._pending_snapshot)
        await self._app.run_async()

    async def wait_ready(self) -> None:
        await self._ready.wait()

    def publish_snapshot(self, snapshot: TerminalSnapshot) -> None:
        app = self._app
        if app is None or not app.is_running:
            self._pending_snapshot = snapshot
            return
        app.call_after_refresh(app.apply_snapshot, snapshot)

    async def close(self) -> None:
        app = self._app
        if app is not None and app.is_running:
            app.exit()
        await asyncio.sleep(0)
