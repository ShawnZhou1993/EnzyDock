# Example files

Small inputs used for manual testing and for the end-to-end test in Step 9.

## `protein/`

* `1UBQ.pdb` — ubiquitin (76 residues, 1.8 Å), downloaded from the RCSB.
  Small enough for a fast docking run and large enough to have a real pocket.

Drop any other `.pdb` / `.cif` / `.mmcif` file here to try it in the GUI.

## `ligand/`

* `aspirin.smi` — `CC(=O)Oc1ccccc1C(=O)O`, a convenient default substrate.
* add `.sdf` / `.mol` / `.mol2` files here to test the file-based route.

> These files are **inputs only**. Every run copies them into a fresh
> `workspace/<timestamp>/input/` folder, so they are never modified.
