# Recurse SDK API reference

Generated from the installed `recurse` package. Do not edit by hand.

## `recurse`

Recurse SDK: author, package, and run Recurse applications.

The public surface is intentionally small:

- :func:`build_bundle` validates an application directory and produces one
  standard source distribution containing its exact dependency lockfile.
- :func:`context` gives application tool code access to the active run's
  validated inputs and writable workspace.
- :class:`RecurseError` and its subclasses report every SDK failure.

The ``recurse`` console command that ships with this distribution handles
login, deployment, and billing; see the package documentation.

## `recurse.BundleError`

The application cannot be built into a valid source distribution.

Raised when project metadata, the selected build backend, or the resulting
source archive does not satisfy the Recurse application contract.

## `recurse.ManifestError`

The application manifest or declared application content is invalid.

The message names the offending ``agent.yaml`` field or declared file so
the author can correct it directly.

## `recurse.RecurseError`

Base class for every error raised by the Recurse SDK.

## `recurse.RunContext`

```python
class RunContext(inputs: collections.abc.Mapping[str, object], workspace: pathlib.Path) -> None
```

A read-only view of the active Recurse run.

Attributes:
    inputs: The validated run inputs, deeply read-only. Mappings are
        immutable mappings and lists are tuples.
    workspace: The writable run output directory. Every file written under
        it is collected as a run artifact when the run finishes.

## `recurse.RunContextError`

The Recurse run context was used outside an active run.

## `recurse.build_bundle`

```python
build_bundle(app_directory: str | pathlib.Path) -> tuple[dict[str, bytes], dict[str, typing.Any]]
```

Validate an application and build its standard deployment artifacts.

The manifest, declared files, and registered tools are validated first;
tool code is parsed but never imported. The application files are then
passed to its declared build backend. The resulting source distribution is
returned unchanged and contains the exact dependency lockfile.

Args:
    app_directory: The application directory containing ``agent.yaml``.

Returns:
    A pair containing source-distribution bytes keyed by ``source`` and a
    portable record with ``apiVersion`` set to
    ``recurse.application/v1alpha1`` and ``source`` containing the exact
    SHA-256 and byte size.

Raises:
    ManifestError: If the manifest, a declared file, or a registered tool
        is invalid.
    BundleError: If project metadata, the build, or its source distribution
        is invalid.

## `recurse.context`

```python
context() -> recurse.RunContext
```

Return the run context of the active Recurse run.

Returns:
    The active :class:`RunContext`.

Raises:
    RunContextError: If no Recurse run is active, for example when the
        application module is imported or executed outside the platform.
