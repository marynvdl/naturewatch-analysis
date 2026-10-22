# Retired

`createCropTiles`, `createBuiltTiles`, and `createBareTiles` used to start a Compute Engine virtual machine on push. The task folders are kept so the scripts are not lost. They are not run.

`workflows/` holds the retired workflow files. They are stubs: they do not start a machine. They stay here, and not in `.github/workflows/`, so GitHub does not run them. Do not move them back. A push runs the workflow in the commit you push, and an older copy of these files did start a machine.

`tasks/fireTiles/` stays where it is. Fire tiles are built by the Fire tiles workflow, started by hand.
