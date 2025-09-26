
# Use DO_TAG

Ensure that when droplets are created, they are given the tag specified in the
DO_TAG config. When looking for droplets that are part of , or should be part
of, the cluster, look for the DO_TAG tag. 

# Info Command

Create a new `cspawn node ` command, `cspawn node info`. 

In a nice table format per section, info will display: 

- details of the main docker host and manager: name, ip. 
- swarm nodes and candidate nodes. 

For the swarm nodes and candidate nodes, get the list of nodes from 'docker node ls` and add the nodes identified by listing the droplets in the DO_PROJECT. Include a column that indicates if the node is:

- Swarm only: Named in the swarm, but is not the name of a running droplet. 
- Cloud only: Listed in the do list of hosts and matches the DO_NAMES template 
- In Swarm: Both listed in running droplets, and also included in the swarm. 


# Purge command

Create a new Cspawn command, `cspawn node purge`

THe purge command will :

* Destroy all of the droplets that are tagged with the DO_TAG tag and are not
part of the swarm,.
* Remove from the swarm all of the nodes that don't have a running droplet. 

the purge command has a -N/--dry-run argument that only prints what would be done. 

# Swarm node labels

A new config variable, `SWARM_NODE_LABEL`, defines a **custom node label** for swarm workers.  

- **When creating new nodes** (e.g., with `cspawn node expand`), assign the label in `SWARM_NODE_LABEL`.  
  Nodes still join the swarm as **workers**.  

- **When creating user code host services** (e.g., with `define_cs_container()`), services are scheduled only on:  
  - worker nodes, and  
  - nodes that have the label defined in `SWARM_NODE_LABEL`.  

This ensures user services always run on workers, but only those intended for the given role.

# Info update

FOr the `cspawnctl node info` command, add to the HostInfo a field to indicate that the node is a manager. Display
this in the output table. 


# Docker contract

Create a new cspawn node command , `cspawnctl node contract' that will find the
node with the highest number and stop it. The node cannot be a swarm manager. 


# Sync Storage 

Create a new cli command `cspawnctl host sync <username>` that will sync the
user storage between the code host and the storage buckets. 

The command will find the service for <username> and via the docker api, 
execute on the service: 

rclone sync "$WORKSPACE_FOLDER" \
  :s3,provider=Other,env_auth=false,access_key_id=$STORAGE_ACCESS_KEY,secret_access_key=$STORAGE_SECRET,endpoint=$STORAGE_ENDPOINT:\
  /users/$JTL_USERNAME/$WORKSPACE_FOLDER \
  --progress


These value will br provided in the command environment from the config: 

- $STORAGE_ENDPOINT
- $STORAGE_ACCESS_KEY
- $STORAGE_SECRET

The remainder are already in the container environment


# Git Push and Pull 

In cspawn.cs_github, add  to GitHubOrg a .get_repo(upstream_url, username)  --
same as fork -- that will return the same StudentRepo as .fork(), but assuming
it is already created. Note that the upstream_url is the upstream to the student
repo, not the student repo. 

Probably should make StudentRepo a regular class, then add to it .push() and
.pull(). These command will work like cspawn.util.host_s3_sync: they will  ssh
into a container and run the commands. 

`config/devel.env` that the required config vars. See cspawn/cs_github/repo.py for
related code that may give you ideas. 

# Remote Push