# quantum-contact-process-1D
## TODO:
- [] (*TenPy*) 2 time evolution algorithms: SingleSiteTDVP and TEBD (SVDBasedTEBDEngine + QRBasedTEBDEngine). Test both
- [] (*TenPy*) WFMC implementation for [higher order](https://www.sciencedirect.com/science/article/pii/S0010465512000835?via%3Dihub)
- [] (*TenPy*) Reproduce density plot in Hendrik's paper
- [] (*scikit_tt*) check SLIM representation, DMRG, TEBD, TDVP
- [] (*scikit_tt*) compute steady-state solution, check Linblad eigenspectrum
- [] (*scikit_tt*) run jobs on HPC, combining with CUDA?
- [] (*QuTip*) check what can be computed for dynamical simulation
- [] (*theory*) Check whether the decay rates are smaller than the minium energy splitting in the system Hamiltonian -> approximations for the [validity](https://qutip.org/docs/latest/guide/dynamics/dynamics-master.html) of Lindblad Master equation
- [] (*theory*) Check Jen's [approach](https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.116.237201), LDPO?
- [] (*technicality*) Putting code also at group's repo `itp0.physik.tu-berlin.de/home/agweimer`? (Check [wiki](https://www3.itp.tu-berlin.de/dokuwiki/agweimer:start))


## Technical details 
Required tools: `conda`, `conda-lock`, `poetry`

### Do this once (create and activate the environment)

```
conda-lock install --name YOURENV linux-64.conda.lock
conda activate YOURENV
make init
```

### Do this happily ever after (Update the environment)

```
# Re-generate Conda lock file(s) based on environment.yml
conda-lock -f environment.yml -p linux-64 -k explicit --filename-template "linux-64.conda.lock"
# Update Conda packages based on re-generated lock file
conda update --file linux-64.conda.lock
# Add new dependencies (from PyPI) and re-generate poetry.lock
poetry add --lock PACKAGE_NAME
# Update the Poetry virtual environment based on the lock file
poetry install
(# To update lock file based on poetry.lock
poetry lock --no-update)
```

#### *Bugs that are not mine ... So try at your own peril anything in []*
- `conda-lock` somehow doesn't work for all platforms [`conda-lock -f environment.yml` or `conda-lock --lockfile conda-lock.yml`] (Something about mamba and conda that don't quite go well together ... see [issue](https://github.com/conda/conda-libmamba-solver/issues/418))
