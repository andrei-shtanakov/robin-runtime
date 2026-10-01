# R16 receipt contract — vendored pinned copy

Source: `andrei-shtanakov/devtools`, `contracts/r16-receipt/v1/` @ `757ea5f`
(devtools#399, devtools#382). Consumed by `src/robin/external_checks.py`.

`v1/` is byte-identical to the source; `tests/test_external_checks.py` pins the
hashes below. Never edit `v1/` in place: a new upstream version arrives as a
sibling `v2/`, Robin accepts both, and only then is the runner told it may write
`schema_version: 2` (README §Эволюция).

```
e59bc5d17dbd737ab0b8bc37b04f59d6623acc7900012ff0eb3536e3dd829420  v1/schema.json
8c2620ee6d10042931b5605f038dc15c8ec9ef3348a7f635c439c853c2be0971  v1/README.md
9bad1a8aa0eb4bc692809a98755e1bfe630f7fa69ee8ab110cb66094e8f39936  v1/examples/completed-ok.json
cf93585212c535fe83fed4cc16c39154373247ef7a7aefca99215fe14336d538  v1/examples/failed.json
4396e0e64abffe098b01acbe1a7e9e9643ffdb5f8e53225077dccd362296b428  v1/examples/missed.json
```
