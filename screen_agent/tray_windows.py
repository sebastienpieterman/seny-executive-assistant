"""
Windows system tray for the Seny Screen Agent.
Must be run from the main thread.
The agent eval loop runs in a background daemon thread.
"""
import threading
import pystray
from PIL import Image, ImageDraw


def _make_icon_image(active: bool) -> Image.Image:
    """Create a simple 64x64 solid-color square icon."""
    color = (0, 180, 0) if active else (150, 150, 150)
    img = Image.new("RGB", (64, 64), color=color)
    # Add a small white circle in the center so it looks like an eye
    draw = ImageDraw.Draw(img)
    draw.ellipse([20, 20, 44, 44], fill=(255, 255, 255))
    draw.ellipse([28, 28, 36, 36], fill=color)
    return img


def run_windows(agent):
    """
    Start the Windows system tray icon.
    Runs agent.run_loop() in a background thread, then blocks on icon.run().
    Call this from the main thread.
    """
    _icon_ref = [None]  # mutable reference so callbacks can access icon

    def toggle_pause(icon, item):
        agent.paused = not agent.paused
        icon.icon = _make_icon_image(active=not agent.paused)
        # Update tooltip
        icon.title = "Seny Screen Agent (Paused)" if agent.paused else "Seny Screen Agent"

    def quit_agent(icon, item):
        icon.stop()

    def pause_label(item):
        return "Resume" if agent.paused else "Pause"

    menu = pystray.Menu(
        pystray.MenuItem(pause_label, toggle_pause),
        pystray.MenuItem("Quit", quit_agent),
    )

    icon = pystray.Icon(
        name="seny_screen_agent",
        icon=_make_icon_image(active=True),
        title="Seny Screen Agent",
        menu=menu,
    )
    _icon_ref[0] = icon

    # Start agent eval loop in background daemon thread
    t = threading.Thread(target=agent.run_loop, daemon=True)
    t.start()

    # Run tray (blocks main thread)
    icon.run()
