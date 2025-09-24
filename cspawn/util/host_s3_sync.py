
class HostS3Sync:
    def __init__(self, app):
        self.app = app
        config = app.app_config
        self.storage_endpoint = config.get('STORAGE_ENDPOINT')
        self.storage_access_key = config.get('STORAGE_ACCESS_KEY')
        self.storage_secret = config.get('STORAGE_SECRET')
        self.storage_bucket = config.get('STORAGE_BUCKET', 'jtl-codeserve-users')

        self.prog_args = 'rclone --log-level ERROR'
        self.local_path = '"$WORKSPACE_FOLDER"'
        self.remote_path =  '$STORAGE_BUCKET/class_$JTL_CLASS_ID/$JTL_USERNAME$WORKSPACE_FOLDER'
     
        self.args = ':s3,provider=DigitalOcean,env_auth=true,endpoint=\'$STORAGE_ENDPOINT\''
        
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
    

    def _run_rclone_op(self, username, op_type, direction, dry_run=False):
        """
        Run /app/bin/rclone.sh with copy|sync in|out
        """
        service, container = self.get_service_and_container(username)
        cmd = f'/app/bin/rclone.sh {op_type} {direction}'
        if dry_run:
            print(f"Would execute on container {container.id[:12]}: {cmd}")
            return
        result = container.o.exec_run(
            cmd=['sh', '-c', cmd],
            environment={
                'STORAGE_BUCKET': self.storage_bucket,
                'STORAGE_ENDPOINT': self.storage_endpoint,
                'AWS_ACCESS_KEY_ID': self.storage_access_key,
                'AWS_SECRET_ACCESS_KEY': self.storage_secret
            },
            user='vscode',
            stream=True,
            demux=True
        )
        if result.output:
            for stdout, stderr in result.output:
                if stdout:
                    print(stdout.decode().strip())
                if stderr:
                    msg = stderr.decode().strip()
                    if "Response has no supported checksum." not in msg:
                        print(f"ERROR: {msg}")

    def sync_to_remote(self, username, dry_run=False):
        # sync out: local -> remote
        self._run_rclone_op(username, 'sync', 'out', dry_run)

    def sync_to_local(self, username, dry_run=False):
        # sync in: remote -> local
        self._run_rclone_op(username, 'sync', 'in', dry_run)

    def copy_to_local(self, username, dry_run=False):
        # copy in: remote -> local
        self._run_rclone_op(username, 'copy', 'in', dry_run)

    def copy_to_remote(self, username, dry_run=False):
        # copy out: local -> remote
        self._run_rclone_op(username, 'copy', 'out', dry_run)

    def has_sync(self, username, class_id):
        """
        Check if the given username has any files in the S3 store under /class_$classid/$username/workspace.
        Returns True if any files exist, False otherwise.
        """
        import minio
        from minio.error import S3Error

       
        # Initialize MinIO client
        minio_client = minio.Minio(
            self.storage_endpoint.replace('http://', '').replace('https://', ''),
            access_key=self.storage_access_key,
            secret_key=self.storage_secret,
            secure=True if self.storage_endpoint.startswith('https://') else False
        )

        # Check if the user has any files in the specified S3 path
        try:
            objects = minio_client.list_objects(self.storage_bucket, f'class_{class_id}/{username}/workspace', recursive=True)
            for obj in objects:
                return True  # Files exist
            return False  # No files found

        except S3Error as e:
            print(f"Error occurred: {e}")
            return False
