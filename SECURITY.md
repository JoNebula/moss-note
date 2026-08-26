# Security

MOSS Note stores uploaded recordings and transcripts on the local filesystem.
The default bind address is `127.0.0.1`; do not expose the application or either
vLLM port directly to the public internet.

For remote access:

1. Put the app behind an HTTPS reverse proxy.
2. Set both `MOSS_AUTH_USERNAME` and a strong `MOSS_AUTH_PASSWORD`.
3. Keep ports 8001 and 8002 bound to localhost or a private container network.
4. Back up and restrict access to the `data/` directory.

To report a vulnerability, open a private GitHub Security Advisory after this
repository is published. Do not include recordings, transcripts, credentials,
or other private data in a public issue.
