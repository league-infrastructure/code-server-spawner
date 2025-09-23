import click

from .root import cli
from .util import get_app


@cli.group()
def telem():
    """Telemetry commands."""
    pass


@telem.command()
@click.pass_context
def summary(ctx):
    """Show a summary of telemetry data from CodeHost records.
    
    Displays the data values that are updated by CodeHost.update_telemetry():
    - Memory usage (system memory in MB)
    - User activity rate (5-minute keystroke average)
    - Utilization averages (30m and 1m keystroke rates)
    - Abbreviated time since last stats, heartbeat, and utilization updates (1s, 2m, 3h, 4d)
    - Additional status columns: Mod (modified_ago), Quiet (is_quiescent), MIA (is_mia), Purge (is_purgeable)
    """
    from tabulate import tabulate
    from cspawn.models import CodeHost
    from datetime import datetime, timezone

    def format_time_ago(delta):
        """Format timedelta as abbreviated string like '1s', '2m', '3h', '4d'"""
        if delta is None:
            return 'N/A'
        
        total_seconds = int(delta.total_seconds())
        
        if total_seconds < 60:
            return f"{total_seconds}s"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            return f"{minutes}m"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            return f"{hours}h"
        else:
            days = total_seconds // 86400
            return f"{days}d"

    app = get_app(ctx)

    with app.app_context():
        # Query all CodeHost records that have telemetry data
        code_hosts = CodeHost.query.filter(
            CodeHost.last_heartbeat.isnot(None)
        ).order_by(CodeHost.last_heartbeat.desc()).all()

        if not code_hosts:
            print("No telemetry data found.")
            return

        # Prepare table data with the values updated by update_telemetry
        table_data = []
        now = datetime.now(timezone.utc)
        
        for host in code_hosts:
            # Format memory usage in MB
            memory_mb = round(host.memory_usage / 1024 / 1024) if host.memory_usage else None
            
            # Calculate time differences and format as abbreviations
            stats_ago = None
            if host.last_stats:
                stats_delta = now - host.last_stats.replace(tzinfo=timezone.utc)
                stats_ago = format_time_ago(stats_delta)

            heartbeat_ago = None
            if host.last_heartbeat:
                heartbeat_delta = now - host.last_heartbeat.replace(tzinfo=timezone.utc)
                heartbeat_ago = format_time_ago(heartbeat_delta)

            utilization_ago = None
            if host.last_utilization:
                utilization_delta = now - host.last_utilization.replace(tzinfo=timezone.utc)
                utilization_ago = format_time_ago(utilization_delta)
            
            # Format modified_ago as abbreviated time
            modified_ago_formatted = f"{host.modified_ago}m" if host.modified_ago is not None else 'N/A'
            
            table_data.append([
                host.service_name or 'N/A',
                host.state or 'N/A',
                memory_mb,
                round(host.user_activity_rate, 3) if host.user_activity_rate is not None else None,
                round(host.utilization_1, 3) if host.utilization_1 is not None else None,
                round(host.utilization_2, 3) if host.utilization_2 is not None else None,
                stats_ago or 'N/A',
                heartbeat_ago or 'N/A',
                utilization_ago or 'N/A',
                modified_ago_formatted,
                '✅' if host.is_quiescent else '',
                '✅' if host.is_mia else '',
                '✅' if host.is_purgeable else ''
            ])

        # Define headers
        headers = [
            'Name',
            'State', 
            'Mem (MB)',
            'Act (5m)',
            'Util 30m',
            'Util 1m',
            'Stats',
            'Heart',
            'Util',
            'Mod',
            'Quiet',
            'MIA',
            'Purge'
        ]

        # Print the table
        print(f"\nTelemetry Summary ({len(code_hosts)} hosts):")
        print(tabulate(table_data, headers=headers, tablefmt='grid'))


@telem.command()
@click.pass_context
def count(ctx):
    """Count the number of telemetry records."""
    app = get_app(ctx)
    count = len(app.csm.keyrate)
    print(f"Total telemetry records: {count}")


@telem.command()
@click.pass_context
def purge(ctx):
    """Purge all telemetry data."""
    app = get_app(ctx)
    app.csm.keyrate.delete_all()
    print("All telemetry data purged successfully")
