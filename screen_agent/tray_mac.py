"""
Mac menu bar tray for the Seny Screen Agent.
Must be run from the main thread (macOS requirement).
The agent eval loop runs in a background daemon thread.
"""
import threading
import rumps


def run_mac(agent):
    """
    Start the Mac menu bar app.
    Runs agent.run_loop() in a background thread, then blocks on rumps.app.run().
    Call this from the main thread.
    """

    class SenyScreenAgentApp(rumps.App):
        def __init__(self, agent):
            super().__init__(name="Seny", title="👁", quit_button=None)
            self._agent = agent
            # Build menu
            self._toggle_item = rumps.MenuItem("⏸  Pause", callback=self._toggle_pause)
            self.menu = [self._toggle_item, None, "Quit"]

        @rumps.clicked("Quit")
        def quit_app(self, _):
            rumps.quit_application()

        def _toggle_pause(self, sender):
            self._agent.paused = not self._agent.paused
            if self._agent.paused:
                sender.title = "▶  Resume"
                self.title = "👁⏸"
            else:
                sender.title = "⏸  Pause"
                self.title = "👁"

    # Start agent eval loop in a background daemon thread
    t = threading.Thread(target=agent.run_loop, daemon=True)
    t.start()

    # Run the Mac tray app (blocks main thread — this is required by macOS)
    app = SenyScreenAgentApp(agent)
    app.run()
