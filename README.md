# Feathub

Feathub is a feature store that facilitates feature development and deployment
to achieve the following objectives:
- **Reduce duplication of data engineering efforts** by allowing new ML projects
  to reuse and share a library of curated production-ready features already
  registered by existing projects in the same organization.
- **Simplify feature management** by allowing users to specify feature
  definitions and feature processing jobs as code using a declarative framework.
- **Facilitate feature development-to-deployment iteration** by allowing users
  to use the same declarative feature definitions across training and serving,
  online and offline, without training-serving skew. Feathub takes care of
  compiling feature definitions into efficient processing jobs and executing those
  jobs in a distributed cluster.

Feathub provides SDK and infra that enable the following capabilities:
- Define feature-view (a group of related features) as transformations and joins
  of the existing feature-views and data sources.
- Register and retrieve feature-views by names from feature registry.
- Transform and materialize features for the given time range and/or keys from the
  feature view into feature stores, by applying transformations on source
  dataset with point-in-time correctness.
- Fetch online features by joining features from online feature store with
  on-demand transformations.

## Architecture

<img src="docs/figures/architecture_1.png" width="60%" height="auto">

<img src="docs/figures/architecture_2.png" width="60%" height="auto">

The above figures show the Feathub architecture. Please checkout [Feathub
architecture](docs/architecture.md) for more details of these components.

## Getting Started

### Prerequisites

Prerequisites for building python packages:
- Unix-like operating system (e.g. Linux, Mac OS X)
- Python 3.7
- Java 8
- Maven >= 3.1.1

### Install Feathub

Run the following command to install Feathub from source.
```bash
# Build Java dependencies for Feathub 
$ cd java
$ mvn clean package
$ cd ..

# Install Feathub
$ python3 -m pip install ./python
```

### Quickstart

#### Quickstart with local processor

Execute the following command to run the
[nyc_tax.py](python/feathub/examples/nyc_taxi.py) demo which demonstrates the
capabilities described above.
```bash
$ python3 python/feathub/examples/nyc_taxi.py
```

#### Quickstart with Flink processor

If you are interested in computing the Feathub features with a local Flink cluster. You
can follow the [Flink Processor Quickstart.](./docs/quickstart_flink_processor.md)

## Additional Resources

- This [tutorial](docs/tutorial.md) provides more details on how to define,
  extract and serve features using Feathub.
- This [document](docs/feathub_expression.md) explains the Feathub expression
  language.
- This [document](docs/flink_processor.md) introduces the Flink processor that computes
  the features with Flink.

## Developer Guidelines

### Install development dependencies

```bash
$ python3 -m pip install -r python/dev-requirements.txt
```

### Running All Tests

```bash
$ pytest -W ignore::DeprecationWarning
```

### Code Formatting

Feathub uses [Black](https://black.readthedocs.io/en/stable/index.html) to format
Python code, [flake8](https://flake8.pycqa.org/en/latest/) to check
Python code style, and [mypy](https://mypy.readthedocs.io/en/stable/) to check type 
annotation.

Run the following command to format codes, check code style, and check type annotation 
before uploading PRs for review.

```bash
# Format python code
$ python3 -m black python

# Check python code style
$ python3 -m flake8 --config=python/setup.cfg python

# Check python type annotation
$ python3 -m mypy --config-file python/setup.cfg python
```
