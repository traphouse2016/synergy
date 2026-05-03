# Synergy Dev Notes

This folder contains developer notes, fix logs, and internal documentation.

Moved here from install root as part of Phase 5 cleanup (fix #48).

## Fix History
- Phase 1: Boot fixes (#4 #23 #24 #56 #26 #73)
- Phase 2: Call flow fixes (#5/#44 #1 #2 #58 #22 #20 #21/#42 #25 #60)
- Phase 3: Reliability (#29 #30 #32 #35 #55 #57 #61 #62 #63 #64)
- Phase 4: UX/Security (#36 #37 #38 #39 #40 #41 #65 #66 #67 #68 #69)
- Phase 5: Code quality (#19 #27 #28 #33 #34 #43 #45 #46 #47 #48 #59 #70 #71 #72 #74 #75 #7)

## Key Conventions
- Profile key format: `gvbot_<emailprefix_stripped_of_special_chars>`
- Driver storage: global `_drivers[profile_key] = selenium_driver`
- Login status: `state["login_status"][profile_key]` = pending / ok / failed
- Flask port: 5050 (hardcoded in both backend and frontend — do not change)
- Profiles dir: `~/synergy/profiles/<profile_key>/`
- Settings file: `~/synergy/settings.json`
- Numbers file: `~/synergy/numbers.txt`

## Do NOT Touch
- `_PLAY_AUDIO_JS` block and `play_audio_in_tab()` DTMF interrupt path — marked FIXED30_GUARDED_PATH
- FFT DTMF listener JS in `_start_dtmf_listen()` — do not alter frequency thresholds
- `_GV_DIAL_JS` script — GV internal React state triggers are fragile
