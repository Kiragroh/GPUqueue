# Security model

This prototype targets **one trusted Windows user on loopback**. It is not a
multi-user inference gateway. Do not bind/proxy it to a public or LAN interface.

The coordinator checks local Host/Origin conditions. Some local inference and
read-only metadata routes intentionally do not require a token. Operator actions
and stored payload/result access have separate authorization. A local process
running as the same Windows user can read `control.token`; that is outside the
isolation this design provides.

DPAPI encrypts request/result bodies and rich context at rest. Ordinary job
metadata, owner/model names and optional `case_id` values can appear in unauthenticated
local status/history. A screenshot-hiding control only changes displayed text;
it is not API access control. Do not use sensitive identifiers as owner/model labels.

No token, database, log, personal model output or real case data belongs in this
repository. All committed examples and test identifiers are synthetic.

No binary release or security certification is provided. Keep endpoint protection
enabled. If it flags a locally built program, stop and use the normal review
process; renaming, repackaging or excluding the file is not a fix.

Please do not put secrets or exploit-bearing private data in public issues.
Use GitHub private vulnerability reporting if enabled on this repository.
