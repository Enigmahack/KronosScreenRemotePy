"""Screen session and connection management."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import json
from pathlib import Path


@dataclass
class SessionInfo:
    """Information about a Kronos connection session."""
    host: str
    port: int
    username: str = ""
    connected: bool = False
    connect_time: Optional[datetime] = None
    stream_fps: int = 15
    pull_mode: bool = False
    device_family: str = "KRONOS"
    device_model: str = ""
    firmware_version: str = ""
    screen_width: int = 800
    screen_height: int = 600

    def duration(self) -> Optional[timedelta]:
        """Get connection duration."""
        if self.connect_time:
            return datetime.now() - self.connect_time
        return None

    def uptime_str(self) -> str:
        """Get formatted uptime string."""
        duration = self.duration()
        if not duration:
            return "Not connected"

        total_seconds = int(duration.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60

        if hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"


@dataclass
class ConnectionSettings:
    """Saved connection settings."""
    host: str
    port: int = 7373
    ctrl_port: int = 7374
    username: str = ""
    remember_password: bool = False
    password: str = ""
    use_pull_mode: bool = False
    max_fps: int = 15
    auto_connect: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = {
            'host': self.host,
            'port': self.port,
            'ctrl_port': self.ctrl_port,
            'username': self.username,
            'use_pull_mode': self.use_pull_mode,
            'max_fps': self.max_fps,
            'auto_connect': self.auto_connect,
        }
        if self.remember_password:
            data['password'] = self.password
        return data

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> ConnectionSettings:
        """Create from dictionary."""
        return ConnectionSettings(
            host=data.get('host', ''),
            port=data.get('port', 7373),
            ctrl_port=data.get('ctrl_port', 7374),
            username=data.get('username', ''),
            remember_password='password' in data,
            password=data.get('password', ''),
            use_pull_mode=data.get('use_pull_mode', False),
            max_fps=data.get('max_fps', 15),
            auto_connect=data.get('auto_connect', False),
        )


class SessionManager:
    """Manage connection sessions and settings."""

    def __init__(self, settings_dir: Optional[Path] = None):
        if settings_dir is None:
            settings_dir = Path.home() / '.kronos_remote'
        self.settings_dir = settings_dir
        self.settings_dir.mkdir(parents=True, exist_ok=True)

        self.current_session = SessionInfo(host="", port=7373)
        self.saved_connections: Dict[str, ConnectionSettings] = {}

        self._load_saved_connections()

    def _load_saved_connections(self):
        """Load saved connections from disk."""
        settings_file = self.settings_dir / 'connections.json'
        if settings_file.exists():
            try:
                with open(settings_file) as f:
                    data = json.load(f)
                    for name, conn_data in data.items():
                        self.saved_connections[name] = ConnectionSettings.from_dict(conn_data)
            except (json.JSONDecodeError, IOError):
                pass

    def _save_connections(self):
        """Save connections to disk."""
        settings_file = self.settings_dir / 'connections.json'
        data = {
            name: settings.to_dict()
            for name, settings in self.saved_connections.items()
        }
        try:
            with open(settings_file, 'w') as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            print(f"Failed to save connections: {e}")

    def save_connection(self, name: str, settings: ConnectionSettings):
        """Save a named connection."""
        self.saved_connections[name] = settings
        self._save_connections()

    def delete_connection(self, name: str):
        """Delete a saved connection."""
        if name in self.saved_connections:
            del self.saved_connections[name]
            self._save_connections()

    def get_connection(self, name: str) -> Optional[ConnectionSettings]:
        """Get a saved connection by name."""
        return self.saved_connections.get(name)

    def list_connections(self) -> list[str]:
        """List all saved connection names."""
        return list(self.saved_connections.keys())

    def start_session(self, host: str, port: int, username: str = ""):
        """Start a new session."""
        self.current_session = SessionInfo(
            host=host,
            port=port,
            username=username,
            connected=True,
            connect_time=datetime.now(),
        )

    def end_session(self):
        """End the current session."""
        self.current_session.connected = False
        self.current_session.connect_time = None

    def get_session(self) -> SessionInfo:
        """Get current session info."""
        return self.current_session

    def update_session_info(self, **kwargs):
        """Update session information."""
        for key, value in kwargs.items():
            if hasattr(self.current_session, key):
                setattr(self.current_session, key, value)


# Global instance
_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """Get the global session manager."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
