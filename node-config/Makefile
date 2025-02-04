ANSIBLE_PLAYBOOK = setup_nfs_mount.yaml
INVENTORY = inventory.ini
ANSIBLE_CFG = ansible.cfg

.PHONY: run
nfs:
	@echo "Running Ansible playbook as root..."
	ANSIBLE_CONFIG=$(PWD)/$(ANSIBLE_CFG) ansible-playbook -i $(INVENTORY) setup_nfs_mount.yaml -u root

.PHONY: check
check:
	@echo "Checking NFS mount on all nodes..."
	ANSIBLE_CONFIG=$(PWD)/$(ANSIBLE_CFG) ansible -i $(INVENTORY) swarm_nodes -u root -m command -a "df -h "

.PHONY: ping
ping:
	@echo "Pinging all Swarm nodes..."
	ANSIBLE_CONFIG=$(PWD)/$(ANSIBLE_CFG) ansible -i $(INVENTORY) swarm_nodes -u root -m ping
