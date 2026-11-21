<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 David Aluu -->

# Shortcut format example

[`sample-example.json`](sample-example.json) is a hand-written illustration of the version 1
shortcut format. It is not copied from a real shortcut and may not match the controls,
element keys, or screen flow on every phone.

Each shortcut file contains `version`, `name`, `aliases`, `goal`, `plan`, `created`, and
`steps`. A step has an `action` object using Phil's action fields and an `expects` array of
stable screen element keys. Keys beginning with `pos:` are not stored. `type` action text
is plain text in the JSON file.
