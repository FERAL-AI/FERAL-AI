# Native persisted profile selection

The native app can retain an explicit personal or preview profile choice for its
next ordinary launch. Previously, using the existing paired profile required an
environment override; an ordinary launch reverted to preview storage.

The versioned preference stores only the profile kind. An explicit FERAL_HOME
still takes precedence. The model resolves the choice once and passes the same
immutable layout to the backend, recovery journal and avatar storage. Personal
selection requires an existing owned directory. Unsupported stored choices fail
closed before startup instead of falling back to another profile.

Settings and onboarding show the active profile and prepare a target review.
Confirmation checks the retained preference domain, previous choice, target
directory identity, idle state and five-minute expiry. Cancellation writes
nothing. Confirmation requires exact preference readback, closes new action
admission and applies only after explicit Quit/reopen. An uncertain write also
closes admission without replaying the save. The old runtime retains its final
conversation save. Chat, voice, coding and pending archive/recovery work prevent
selection. Prior-profile imported avatars are not displayed under the new root;
existing display preferences are preserved.

The parent integrated ten ordinary files after verifying all eight baseline and
ten payload hashes. The parent affected gate passed 87 layout assertions, 61
selection assertions and 23 linked selection assertions within 30 model wire
groups, plus 39 desktop and five error-presentation assertions. Actual modified
model/runtime/views compile in that gate; preferences, metadata and HTTP are
inert fixtures. Existing Swift 5 asynchronous NSLock warnings remain.

This is not actual GUI persistence or a universal profile-writer lease. Candidate
9.63 requires a fresh 55-input optimized compile and live save/Quit/ordinary-reopen
verification. Early cooperating backend writer exclusion is separate pending
integration. Legacy and offline writers, phone reconnection, vault/TCC behavior,
distribution signing and clean-machine installation require their own evidence.
No profile was copied, reset or migrated by these fixtures.
