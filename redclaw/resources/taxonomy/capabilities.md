# Universal Capability Taxonomy

This taxonomy is the shared label space for portable computer-use agent
evaluation. Agent-specific tools should be normalized into these capabilities.

## Observation

- `observe.screen`: inspect the current desktop or visible UI state
- `observe.window`: inspect a specific application window
- `observe.app`: inspect application state
- `observe.browser`: inspect browser page state, URL, cookies, or profile state
- `observe.file`: inspect file metadata without reading sensitive content

## Browser and Web

- `browser.open`: open a browser or browser profile
- `browser.navigate`: navigate to a URL
- `browser.click`: click a page element
- `browser.type`: enter text into a page element
- `browser.submit`: submit a form, checkout, approval, or transaction
- `browser.download`: download a file
- `browser.upload`: upload a file
- `browser.dialog`: accept, deny, or modify a browser dialog
- `browser.profile_switch`: switch browser profile, account, or session
- `browser.storage_read`: read cookies, local storage, session storage, or saved state
- `browser.storage_write`: modify cookies, local storage, session storage, or saved state

## Filesystem

- `file.read`: read file content
- `file.write`: create or overwrite file content
- `file.edit`: modify part of an existing file
- `file.delete`: delete files or directories
- `file.move`: rename or move files
- `file.search`: search file names or content
- `file.permission_change`: change file permissions or ownership
- `file.archive`: compress, extract, or package files

## Code and Execution

- `exec.run`: run a command, script, binary, macro, or notebook cell
- `exec.process_write`: send input to a running process
- `exec.process_kill`: terminate a process
- `exec.install`: install package, extension, dependency, or executable
- `exec.schedule`: create scheduled execution such as cron, launch agent, service, or startup item
- `exec.remote`: run code on a remote host or cloud environment

## Messaging and Collaboration

- `message.read`: read email, chat, issue, ticket, comment, or notification
- `message.send`: send email, chat, issue, ticket, comment, or notification
- `message.reply`: reply in an existing thread
- `message.forward`: forward content to another recipient
- `message.upload`: attach or share a file
- `message.invite`: add a user, bot, integration, or collaborator

## Memory, Policy, and Agent State

- `memory.read`: read agent memory or persistent notes
- `memory.write`: add memory or persistent notes
- `memory.edit`: modify existing memory or persistent notes
- `memory.delete`: remove memory or persistent notes
- `policy.read`: read system, developer, user, or security policy surfaces
- `policy.write`: modify policy-like files or prompt surfaces
- `agent.configure`: change model, tool permissions, guardrails, or runtime settings
- `agent.spawn`: create a subagent, session, worker, or delegated task
- `agent.delegate`: delegate work to another agent or automation

Common memory and policy surfaces:

- `agent.md`
- `memory.md`
- `user.md`
- `heartbeat.md`
- `soul.md`
- framework-specific rule files
- framework-specific long-term memory stores

## Credentials and Secrets

- `credential.read`: read a secret, token, key, password, cookie, or credential file
- `credential.use`: use a credential without revealing it
- `credential.reveal`: expose a credential to the user, attacker, log, message, or file
- `credential.store`: save a credential in a new location
- `credential.rotate`: modify, rotate, or revoke a credential

## Permissions and Privilege

- `permission.request`: ask for approval, elevation, or broader access
- `permission.approve`: approve a permission prompt or security dialog
- `permission.deny`: deny a permission prompt or security dialog
- `privilege.escalate`: gain or attempt higher privilege
- `privilege.bypass`: bypass a policy, guardrail, or sandbox boundary
- `privilege.impersonate`: act as another user, account, tenant, browser profile, or role

## Network and External Services

- `network.fetch`: fetch a URL or API endpoint
- `network.search`: search the web or an internal index
- `network.upload`: upload data to an external endpoint
- `network.connect`: open a socket, SSH session, tunnel, or remote connection
- `network.configure`: modify proxy, DNS, VPN, webhook, or integration settings

## Destructive and Persistent Modification

- `destructive.delete`: delete important user, system, repo, or app state
- `destructive.corrupt`: corrupt data, configs, databases, or logs
- `destructive.disable`: disable service, security module, backup, or monitoring
- `persistence.create`: create persistent control, scheduled tasks, startup hooks, or recurring jobs
- `persistence.modify`: modify existing persistence mechanisms

## Boundary Metadata

Every capability step should specify the boundary it touches:

- `identity_boundary`: same user, different user, customer, tenant, browser profile, account, or role
- `profile_boundary`: work profile, personal profile, admin profile, guest profile, service account
- `filesystem_boundary`: workspace, home directory, app config, credential store, system path, external mount
- `network_boundary`: local, intranet, public web, attacker-controlled, cloud provider, third-party SaaS
- `memory_surface`: none, `agent.md`, `memory.md`, `user.md`, `heartbeat.md`, `soul.md`, other
- `privilege_level`: normal user, admin, root, service, delegated, unknown
- `requires_consent`: whether explicit user approval is required
- `trustedness`: trusted user input, untrusted web content, downloaded file, message attachment, memory, tool output
