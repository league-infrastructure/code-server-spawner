INVENTORY = inventory.ini
ANSIBLE_CFG = ansible.cfg

#
# Run commands on each node. Just uses ansible to ssh to each. 

.PHONY: check ping remount nfs nodes labels

check:
	@echo "Checking NFS mount on all nodes..."
	ANSIBLE_CONFIG=$(PWD)/$(ANSIBLE_CFG) ansible -i $(INVENTORY) swarm_nodes -u root -m command -a "df -h"

ping:
	@echo "Pinging all Swarm nodes..."
	ANSIBLE_CONFIG=$(PWD)/$(ANSIBLE_CFG) ansible -i $(INVENTORY) swarm_nodes -u root -m ping

remount:
	@echo "Re-mounting NFS on all nodes..."
	ANSIBLE_CONFIG=$(PWD)/$(ANSIBLE_CFG) ansible -i $(INVENTORY) swarm_nodes -u root -m command -a "sudo mount -o remount /mnt/student_repos"

#
# Run Playbooks

nfs:
	ANSIBLE_CONFIG=$(PWD)/$(ANSIBLE_CFG) ansible-playbook -i $(INVENTORY) setup_nfs_mount.yaml -u root

nodes: 
	ANSIBLE_CONFIG=$(PWD)/$(ANSIBLE_CFG) ansible-playbook -i $(INVENTORY) swarm_nodes.yaml -u root

fw:
	ANSIBLE_CONFIG=$(PWD)/$(ANSIBLE_CFG) ansible-playbook -i $(INVENTORY) swarm_nodes_fw.yaml -u root


labels:
	@echo "Labels for all nodes..."
	@bash -c 'docker node ls -q | xargs docker node inspect -f "{{ .ID }} [{{ .Description.Hostname }}]: {{ .Spec.Labels }}"'

