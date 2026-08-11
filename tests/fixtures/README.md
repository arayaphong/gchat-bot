# Chat history event fixtures

The `chat_history_addon_*` fixtures model the Google Workspace Add-on event
contract documented by Google. Resource IDs and action handles are synthetic.
The three button fixtures intentionally differ only by
`commonEventObject.platform` so the event normalizer cannot make authorization
decisions from the client platform.

`chat_history_rest_confirmation_message.json` models the message returned by
REST create/get. Its custom client message ID intentionally matches every
button fixture so contract tests lock the required REST-to-callback binding.

`chat_history_legacy_message.json` is derived from the field shape observed in
the project's authenticated legacy event log. All values were replaced; no
message content, user name, space ID, thread ID, token, or attachment reference
from the log is retained.

These fixtures are contract inputs, not proof that every Google Chat client
currently emits byte-for-byte identical JSON. Web, Android, and iOS callback
payloads must be captured through the application's redaction path during the
Phase 5 staging acceptance test before destructive rollout.
