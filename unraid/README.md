# Unraid template

[Download eden.xml](https://raw.githubusercontent.com/Crunch41/eden-room-docker/main/unraid/eden.xml) for the custom `crunch41/eden-room-server:latest` image. This is a repository-hosted template, not a claim of Community Apps listing or approval.

## Community Apps comparison

Reviewed 7 October 2026. No matching dedicated-room template was found in the Community Apps search. The desktop Eden emulator listing is a different application. This template follows this repository entrypoint: UDP 24872, persistent room data, PUID/PGID and room options. Private mode is the default; public announcements require USERNAME, TOKEN and WEB_API_URL. No account values are included.

## Using it

For a new installation, download the XML into `/boot/config/plugins/dockerMan/templates-user/` with a `my-` filename, then choose it in Docker > Add Container. Review every storage path, port, permission and credential before Apply. These are generic defaults, not a backup of an existing installation.

For an existing container, keep its installed XML, repository, network, ports, credentials and mounts. Updating only its `TemplateURL` to the raw link above connects source monitoring without recreating the container. Do not overwrite its XML with this generic file.

The template checker tracks changes to this custom template. Community-template changes remain a separate reference for review; application upstream changes are handled by this repository's existing build workflow.

Validation covers XML structure, image identity, required runtime fields, blank secrets and the checker's parsing/fingerprinting. A fresh installation from the generic template has not been exercised; existing services are not recreated by a metadata-only update.
