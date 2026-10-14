from pathlib import Path

from rich.console import Console
from rich.table import Table

from ..backups import BackupError, BackupManager
from .prompts import Choice, confirm, select, text

console = Console()


def backup_menu(service, exporter, export_path: Path):
    manager = BackupManager(service.db)
    while True:
        console.clear()
        console.print("[bold]Backups and recovery[/bold]")
        console.print(f"Daily snapshots are saved in {manager.backup_dir}")
        action = select("Choose:", [
            Choice("Create backup now", "create"),
            Choice("View backups", "list"),
            Choice("Recover missing history from a backup", "recover"),
            Choice("← Back", None),
        ])
        if action is None:
            return
        if action == "create":
            try:
                path = manager.create_backup()
                console.print(f"[green]Backup saved:[/green] {path}")
            except (BackupError, OSError) as exc:
                console.print(str(exc), style="red", markup=False)
            input("Press Enter to continue...")
            continue
        backups = manager.list_backups()
        if action == "list":
            table = Table(title="Saved snapshots")
            for title in ("Created", "Reason", "Sessions", "Results", "File"):
                table.add_column(title)
            for info in backups:
                table.add_row(info.created_at or "—", info.reason, str(info.sessions) if info.valid else "INVALID", str(info.entries) if info.valid else info.error, info.path.name)
            console.print(table)
            if not backups:
                console.print("No backups yet.")
            input("Press Enter to continue...")
            continue
        choices = [Choice(f"{b.created_at} • {b.reason} • {b.sessions} sessions / {b.entries} results", b.path) for b in backups if b.valid]
        choices.extend([Choice("Choose another snapshot file", "custom"), Choice("← Back", None)])
        selected = select("Recover missing history from:", choices)
        if selected is None:
            continue
        if selected == "custom":
            raw = text("Snapshot database path:")
            if not raw:
                continue
            selected = Path(raw.strip()).expanduser()
        console.print("Recovery adds missing workouts and results. Existing workouts, corrections, and settings are kept. A backup is created before recovery.")
        if not confirm("Recover missing history from this snapshot?", default=False):
            continue
        try:
            result = manager.recover_backup(selected)
            console.print(f"[green]Recovered {result.sessions} workouts, {result.entries} results, and {result.bodyweights} weigh-ins.[/green]")
            console.print(f"Backup before recovery: {result.pre_restore_backup}")
            try:
                exporter.export(export_path)
                console.print("[green]Excel updated.[/green]")
            except Exception as exc:
                console.print(f"History is safely stored; Excel export failed: {exc}", markup=False)
        except (BackupError, OSError) as exc:
            console.print(str(exc), markup=False)
        input("Press Enter to continue...")
