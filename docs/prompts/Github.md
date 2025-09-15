# Github access

The Github acess feature forks source repos into a student Github organization,
with a repo spcific to the student. This feature is only enabled if the Github
config vars are set: 

* GITHUB_ORG. The URL of the student Github organization
* GITHUB_TOKEN. A Personal Access Token for repos in the student org, Provided
  to the student code host
* GITHUB_ORG_TOKEN. Private, used for forking and deleting. 

When a new Code Host is created, the repo for the students host is first forked
into the organization specified by the config var GITHUB_ORG, then it is the
forked repo that is cloned into the Code Host. The name of the repo is the
original name of the upstream repo, with the users username appended. 

## Sprints

### Sprint 1

Create the module dir cs_github and file cs_github/repo.py, with the class
GithubOrg and StudentRepo,  which represents the GithubOrg specified in
GITHUB_ORG and the students fork of the repo in the organization. 

GitHubOrg methods include:

* Fork an upstream repo into the organization, renaming it to include the students username
* Return a GithubRepo given the name of the upstream  and a username. 


For instance, if the upstream repo ( referenced in the Class Prototype ) is 

https://github.com/league-curriculum/Python-Apprentice 

and `GITHUB_ORG` is https://github.com/League-Students and the user name is `student`, 
then GitHubOrg would fork to:

https://github.com/League-Students/Python-Apprentice-student


#### CLI

Create a new cli module in `cspawn.cli.github` that has these commands: 

* cspawnctl github fork --repo=<repo_url> <user_name> # Fork into the GITHUB_ORG
* cspawnctl github fork --class=<class_id> <user_name> # Use the repo of the class prototype
* cspawnctl github rm --repo=<repo_url> <user_name> # Delete the repo
* cspawnctl github rm --class=<class_id> <user_name> # Use the repo of the class prototype