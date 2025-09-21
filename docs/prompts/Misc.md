
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
