We need to build a pipeline or downloading all available IMERG data. We will work through each task one-at-a-time together.

# 1. init CLAUDE.md for the codebase [COMPLETED]

# 2. init a git repo [COMPLETED]
Initalize a github repo and connect to the remote (see general notes).

# 3. Plan and build the codebase
I want you to build the codebase for this pipeline. Design the codebase to be consistent with each of the other access codebases (see general notes).
Download the data using the earthaccess python package.
The environment is already created.
See the search notebook for an example of searching the data.
Make sure that the version, product (in this case: [early, late, final]), and time frequency are configurable - I will eventually want to download all of them.
At first, set up the scripts for downloading version 07 final daily files. We will configure for other products in the future.


# General notes:
- the git remote is: https://github.com/kheyblom/access_imerg.git
- ensure to always use proper git version controlling as changes are made
- use and update a CLAUDE.md that will take this information and effectively and efficiently handle this project
- use information learned in each of the directories in: @/glade/u/home/kheyblom/work/data_access and add to this project's CLAUDE.md. Each directory contains a codebase for accessing data to be downloaded into our data lake. @/glade/u/home/kheyblom/work/data_access/access_smap will ikely be the most useful as is also uses the earthacess package.
- it is always important to minimize compute costs on derecho. we have a finite allocation and we need to managing our usage.
- keep notes on your current working state. you may lose connection to the HPC system or I may need to start new sessions, so I need you to be able to easily pick up where you left off.
- additional tasks may come up that need to occur between the above tasks. this task list can be flexible, but if substantial changes are need, I need to approve them.
- mark tasks as completed when they are completed