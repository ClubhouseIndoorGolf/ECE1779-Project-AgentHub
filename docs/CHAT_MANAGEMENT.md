# Chat management

Right-click a saved conversation or use its **⋯** button. Keyboard users can
focus the conversation and press Shift+F10, or focus the actions button and press
Enter. Arrow keys navigate the menu; Escape dismisses it. Rename and Delete use
native dialogs. Delete focuses Cancel initially and requires confirmation.

- **Rename:** save a nonblank title of up to 200 characters. Unicode is preserved.
- **Pin / Unpin:** pinned chats sort first, then chats sort by latest update.
- **Delete:** remove a settled chat from normal history. Refund records, messages
  and audit events remain in the database; this is not a personal-data erasure
  endpoint. There is no restore action in the UI. Active jobs and unexpired
  proposals awaiting approval block deletion with a clear message. Finish the
  job or reject the proposal first. Already applied refunds are never reversed.

`PATCH /conversations/{id}` accepts explicit `title` and/or `is_pinned` values.
`DELETE /conversations/{id}` returns 204, including repeated deletion by the same
owner. Other-account and unknown IDs return 404. Deleted chats cannot accept new
messages or decisions; their history, job and steps endpoints return 404. Existing
SSE connections close with `conversation.deleted`, clearing that chat in the UI.

Migration `0009_conversation_management` adds `is_pinned` (default false) and
nullable `deleted_at`. Apply it before deploying this API/worker release. It
preserves existing records and refuses downgrade if removed chats would reappear.
No existing migration is rewritten. Deploy API replicas together before enabling
deletion: an older API does not understand the removal flag.

To check manually, create a disposable empty chat, rename it, pin it, and reload
and sign in again to verify persistence. Open Delete and cancel to verify nothing
changes, then confirm deletion on that disposable chat. Try deleting a chat with
a pending proposal and verify that it remains visible with an explanatory error.

Local verification on changes based on `f8d0ddf`: 192 backend tests and 27 offline
browser-state tests passed, including ownership, pin ordering, validation, retained
refund records, stream closure and migration from both prior schema branches.
