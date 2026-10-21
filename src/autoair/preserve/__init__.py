"""Source-preserving editing of Python DAG files (LibCST) and ejected-file detection.

``DagSpec`` stays the single logical model: LibCST is only used at the boundary, to map a
file (:mod:`.mapper`) and to patch it (:mod:`.patcher`); :mod:`.service` decides the source
policy, picks the file to patch, verifies the patch with a round trip and reports what a
deploy would write; :mod:`.fingerprint` marks generated files so hand edits are detected.
"""
