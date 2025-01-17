from setuptools import find_packages, setup

setup(
    name="recurrent-intensity-model-experiments",
    version="1.0",    # will be overwritten by use_scm_version
    packages=find_packages(where="src", exclude=("test",)),
    package_dir={"": "src"},

    # This list of requirements work in pytorch_latest_p37 jupyter notebook environments,
    # with the exception that torch, dgl, and implicit should be manually installed beforehand.
    # To work in github, we need more packages via `conda env update --file environment.yml`
    install_requires=[
        "torch>=1.7.1",  # torch==1.7.1+cu101
        "pytorch-lightning>=1.3.8",
        "numba>=0.52.0",
        "lightfm>=1.16",
        "pyarrow>=0.13.0",
        "tick>=0.6",
        # "implicit>=0.4.4", # conda install -c conda-forge implicit implicit-proc=*=gpu -y
        # "dgl",  # pip install dgl-cu111 # or matching cuda version with torch
        "transformers>=4.12.2",  # optional for zero_shot models
        "seaborn>=0.11.1",
        "scipy>=0.19",
        "scikit-learn>=0.24.2",
        "pandas>=1.1.5",
        "matplotlib>=3.3.4",
        "attrdict>=2.0.1",
        "psutil",
    ],
)
