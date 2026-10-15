# Recurse SDK API reference

Generated from the installed `recurse` package. Do not edit by hand.

## `recurse`

Recurse SDK: author, package, and run Recurse applications.

The public surface is intentionally small:

- :func:`build_bundle` validates an application directory and produces one
  standard source distribution containing its exact dependency lockfile.
- :func:`load_tools_manifest` validates a tools-only declaration.
- :func:`build_tools_bundle` builds tools-only source without a prompt or agent.
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

The message names the offending ``agent.yaml`` or ``tools.yaml`` field or file so
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

## `recurse.ToolsClient`

```python
class ToolsClient(access_token: str, *, base_url: str | None = None) -> None
```

Call the tools service from a local login or an explicit server bearer.

The explicit constructor never reads a keychain or opens a browser. Supply a
short-lived bearer for the exact tools ``/mcp`` resource; renew it in the calling
server process. Use :meth:`from_login` explicitly for the saved local CLI login.
Methods return the service's ordinary JSON objects and durable call receipts.

``catalog_search`` pages the catalog; ``catalog_get`` inspects schemas, versions
and price. ``call`` requires those versions, JSON inputs, a stable idempotency
key and an explicit cost cap. ``get_call`` reads status/results/charges without
another admission; ``cancel_call`` requests cancellation. Execution failures
and unresolved billing remain receipt data. ``publish`` builds and publishes
source for the authorized publisher. ``prepare_private`` prepares a customer's
own project without publication; ``run`` prepares, inspects and admits a private
call. ``get_version`` inspects publisher preparation;
``publish_version`` activates an already prepared version.
``apis_search`` and ``apis_get`` inspect approved upstream read APIs without
exposing credentials. Private functions declare their permitted IDs in ``apis``.
``from_login`` uses
the saved local login and can refresh one rejected resource bearer. An explicit
server bearer is never refreshed or read from local credentials automatically.

## `recurse.ToolsError`

A tools request, local login, source preparation or publication could not complete.

Execution failures are returned as durable call receipts instead of exceptions.

Attributes:
    status_code: HTTP rejection status, or ``None`` for a local/transport failure.
    version_id: Allocated source version to inspect or resume, when known.

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

## `recurse.build_tools_bundle`

```python
build_tools_bundle(service_directory: str | pathlib.Path) -> tuple[dict[str, bytes], dict[str, typing.Any]]
```

Validate tools-only source and build one immutable standard distribution.

Tools are parsed without importing them on the author's machine. The
distribution contains ``tools.yaml``, its source, project metadata, and
the exact dependency lockfile. No agent manifest or prompt is required.

Args:
    service_directory: Directory containing ``tools.yaml`` and project files.

Returns:
    Source-distribution bytes keyed by ``source`` and a portable record
    with ``apiVersion`` set to ``recurse.tools.application/v1alpha1``.
    The record's ``source`` contains the exact SHA-256 and byte size.

Raises:
    ManifestError: If declarations, files, schemas, or signatures are invalid.
    BundleError: If the directory, build metadata, backend output, or
        dependency lockfile does not satisfy the tools artifact contract.

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

## `recurse.load_tools_manifest`

```python
load_tools_manifest(service_directory: str | pathlib.Path) -> dict[str, typing.Any]
```

Read a tools-only declaration without importing its Python source.

Args:
    service_directory: Directory containing ``tools.yaml``.

Returns:
    The validated, JSON-portable manifest exactly as authored.

Raises:
    ManifestError: If the manifest is unreadable, malformed, or violates
        the tools-only contract, including schema and fixture pricing.
