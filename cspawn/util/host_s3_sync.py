class HostS3Sync:
    def __init__(self, app):
        self.app = app
        from cspawn.cli.util import get_config
        config = get_config()
        self.storage_endpoint = config.get('STORAGE_ENDPOINT')
        self.storage_access_key = config.get('STORAGE_ACCESS_KEY')
        self.storage_secret = config.get('STORAGE_SECRET')
        self.storage_bucket = config.get('STORAGE_BUCKET', 'jtl-codeserve-users')

        self.user_subdir = 'users'

        if not all([self.storage_endpoint, self.storage_access_key, self.storage_secret, self.storage_bucket]):
            raise ValueError("Missing storage configuration. Required: STORAGE_ENDPOINT, STORAGE_ACCESS_KEY, STORAGE_SECRET, STORAGE_BUCKET")

    def get_service_and_container(self, username):
        service = self.app.csm.get_by_username(username)
        if not service:
            raise ValueError(f"No service found for username: {username}")
        containers = list(service.containers)
        if not containers:
            raise ValueError(f"No containers found for service {service.name}")
        return service, containers[0]

    def sync_host(self, username, dry_run=False):
        service, container = self.get_service_and_container(username)
        print(f"Found service {service.name} for username {username}")
        print(f"Using container {container.id[:12]} on node {container.node_name()}")
        cmd = (
            'rclone sync "$WORKSPACE_FOLDER" '
            '":s3,provider=DigitalOcean,env_auth=false,'
            'access_key_id=$STORAGE_ACCESS_KEY,secret_access_key=$STORAGE_SECRET,'
            'endpoint=\'$STORAGE_ENDPOINT\':$STORAGE_BUCKET/class_$JTL_CLASS_ID/$JTL_USERNAME$WORKSPACE_FOLDER"'

        )


        if dry_run:
            print(f"Would execute on container {container.id[:12]}: {cmd}")
            return
        print(f"Executing sync command on container {container.id[:12]}")
        print(f"Command: {cmd}")
        result = container.o.exec_run(
            cmd=['sh', '-c', cmd],
            environment={
                'STORAGE_BUCKET': self.storage_bucket,
                'STORAGE_ENDPOINT': self.storage_endpoint,
                'STORAGE_ACCESS_KEY': self.storage_access_key,
                'STORAGE_SECRET': self.storage_secret
            },
            stream=True,
            demux=True
        )
        if result.output:
            for stdout, stderr in result.output:
                if stdout:
                    print(stdout.decode().strip())
                if stderr:
                    print(f"ERROR: {stderr.decode().strip()}")
        print(f"Sync completed with exit code: {result.exit_code}")

    def has_sync(self, username, class_id):
        """
        Check if the given username has any files in the S3 store under /class_$classid/$username/workspace.
        Returns True if any files exist, False otherwise.
        """
        import subprocess
        s3_path = f":s3,provider=DigitalOcean,env_auth=false,access_key_id={self.storage_access_key},secret_access_key={self.storage_secret},endpoint='{self.storage_endpoint}':{self.storage_bucket}/class_{class_id}/{username}/workspace"
        # Use rclone ls to list files in the path
        cmd = [
            "rclone", "ls", s3_path
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            # If any output, files exist
            if result.stdout.strip():
                return True
            return False
        except Exception as e:
            print(f"Error checking sync for user {username}, class {class_id}: {e}")
            return False
