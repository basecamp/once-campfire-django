# Frontend provenance

`reference/` provides the immutable original application's JavaScript, CSS, images and sounds.
`sources/vendor/` contains the framework frontend distributions needed to build that importmap;
its manifest and individual license files describe their versions. These assets were copied
from the completed public Rust port's asset package. No Rust code or binaries are required.

`bin/build-assets` builds Propshaft digests and the importmap using Python. Its algorithms were
adapted from the public Go port. Port-owned repairs shadow logical paths in `overrides/`.
Generated files are ignored and rebuilt locally or inside the production image.
