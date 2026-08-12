variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for resources"
  type        = string
  default     = "us-central1"
}

variable "github_owner" {
  description = "GitHub repository owner"
  type        = string
  default     = "alexenos"
}

variable "github_repo" {
  description = "GitHub repository name"
  type        = string
  default     = "teetime"
}

variable "github_branch" {
  description = "GitHub branch to trigger deployments"
  type        = string
  default     = "main"
}

variable "cloud_run_memory" {
  description = <<-EOT
    Memory allocation for Cloud Run service.

    Headless Chrome peaks around 1 GiB on its own while a booking page with
    150+ slots is loaded, on top of the always-on FastAPI process and Discord
    gateway. At 1Gi the container was OOM-killed mid-booking (2026-08-02),
    losing the result notification. Do not lower this below 2Gi.
  EOT
  type        = string
  default     = "2Gi"

  # The default only applies when a caller omits the variable; a tfvars override
  # could still set 1Gi and silently reintroduce the OOM. Reject that outright.
  #
  # Normalises to MiB and compares once. Written as a single try() because
  # Terraform does not guarantee short-circuit evaluation of && - a malformed
  # value must fall through to false rather than erroring out of the check
  # itself. Avoids endswith(), which needs Terraform 1.3 (this module allows 1.0).
  validation {
    condition = try(
      tonumber(regex("^([0-9]+)(Mi|Gi)$", var.cloud_run_memory)[0]) *
      (regex("^([0-9]+)(Mi|Gi)$", var.cloud_run_memory)[1] == "Gi" ? 1024 : 1) >= 2048,
      false
    )
    error_message = "cloud_run_memory must be at least 2Gi (or 2048Mi), formatted like \"2Gi\" or \"2048Mi\". Headless Chrome OOM-killed the container at 1Gi, losing the booking result."
  }
}

variable "cloud_run_cpu" {
  description = "CPU allocation for Cloud Run service"
  type        = string
  default     = "1"
}

variable "cloud_run_max_instances" {
  description = "Maximum number of Cloud Run instances. Must be 1 while the Discord gateway runs in-process: a second instance would open a second gateway session and double-process every message."
  type        = number
  default     = 1
}

variable "cloud_run_min_instances" {
  description = "Minimum number of Cloud Run instances. Must be 1 while the Discord gateway runs in-process (a persistent WebSocket needs an always-on instance; with cpu_idle=false this bills roughly USD 50/month for 1 vCPU + 1GiB)."
  type        = number
  default     = 1
}

variable "timezone" {
  description = "Timezone for the application"
  type        = string
  default     = "America/Chicago"
}

variable "booking_open_hour" {
  description = "Hour when booking opens (24-hour format)"
  type        = number
  default     = 6
}

variable "booking_open_minute" {
  description = "Minute when booking opens"
  type        = number
  default     = 30
}

variable "days_in_advance" {
  description = "Number of days in advance bookings can be made"
  type        = number
  default     = 7
}

variable "enable_cloud_sql" {
  description = "Enable Cloud SQL PostgreSQL instance (adds ~$7-10/month)"
  type        = bool
  default     = true
}

variable "cloud_sql_tier" {
  description = "Cloud SQL instance tier"
  type        = string
  default     = "db-f1-micro"
}

variable "cloud_sql_disk_size" {
  description = "Cloud SQL disk size in GB"
  type        = number
  default     = 10
}

variable "container_image" {
  description = "Container image to deploy (passed from Cloud Build)"
  type        = string
  default     = ""
}

variable "messaging_channel" {
  description = "User messaging channel: 'discord' (gateway in-process, requires min/max instances = 1 and always-on CPU) or 'twilio'"
  type        = string
  default     = "discord"

  validation {
    # Matched exactly (not case-folded) by both the cpu_idle expression here
    # and settings.messaging_channel in the app, so a near-miss like "Discord"
    # would silently run with no gateway at all.
    condition     = contains(["discord", "twilio"], var.messaging_channel)
    error_message = "messaging_channel must be exactly \"discord\" or \"twilio\" (lowercase)."
  }
}

variable "log_level" {
  description = "Application log level (DEBUG to see BOOKING_DEBUG messages)"
  type        = string
  default     = "INFO"
}

variable "walden_direct_http_booking" {
  description = <<-EOT
    Run the booking chain as direct PrimeFaces HTTP calls instead of browser
    clicks. Login, navigation and slot discovery still run in Chrome; only the
    chain itself moves to HTTP, and a failure before the reservation is
    submitted falls back to the JavaScript chain.

    On, together with walden_fast_booking_immediate, so the path can be
    validated against the live site off-race. Set both to false to revert to
    the browser chain everywhere.
  EOT
  type        = bool
  default     = true
}

variable "walden_refresh_view_at_window" {
  description = <<-EOT
    Re-render the tee sheet at 6:30:00 and fire Reserve against that render,
    instead of against the one the request was staged from ~60s earlier.

    Off, having been tried. It was built on the reading that the club refuses a
    Reserve staged before the window for being stale. On 2026-08-07 it worked
    exactly as designed - fresh sheet, countdown gone, 86 of 87 rows offering a
    Reserve - and the club refused anyway, with the same ViewState and component
    id the staged request already carried. It cost 730ms of a race decided in
    the first second and changed no byte of the request.

    Turn on only if a morning shows a genuine stale-view refusal.
  EOT
  type        = bool
  default     = false
}

variable "walden_adhoc_execute_delay_s" {
  description = <<-EOT
    Seconds an ad-hoc booking waits before firing Reserve, instead of firing as
    soon as the tee sheet is staged.

    The wait is the point. Every timed-path behaviour - slot pre-location,
    clock-skew probing, the precision wait, and a session left to age before
    Reserve goes out - is gated on execute_at being set, not on the hour being
    06:30. Firing ad-hoc immediately left the machinery that decides the only
    booking that matters exercised once a day, unobserved, against slots a
    dozen members were racing us for.

    With a delay, a Tuesday-afternoon booking nobody wants runs the whole race
    path, where a refusal cannot be another member and so is one we caused.

    90s brackets the ~68s the 6:30 job stages ahead. Sweep it (15/60/120/300)
    to find out whether refusals track the length of the wait. 0 restores the
    old fire-immediately behaviour.
  EOT
  type        = number
  default     = 90

  validation {
    # Whole seconds, and not negative. The setting is read into an `int`, so a
    # fractional value reaches the container as "90.5" and stops it booting -
    # a deploy-time failure for something a plan can catch. The upper bound is
    # a sanity rail: past ten minutes the ad-hoc booking has stopped being a
    # booking, and Cloud Run's request budget is not the place to find out.
    condition     = var.walden_adhoc_execute_delay_s == floor(var.walden_adhoc_execute_delay_s) && var.walden_adhoc_execute_delay_s >= 0 && var.walden_adhoc_execute_delay_s <= 600
    error_message = "walden_adhoc_execute_delay_s must be a whole number of seconds between 0 and 600."
  }
}

variable "walden_adhoc_untimed_retry" {
  description = <<-EOT
    After an ad-hoc booking is refused on the timed path, re-attempt it on the
    untimed one - a fresh session firing Reserve at once, which is what ad-hoc
    bookings did before the delay existed.

    Keeps ad-hoc bookings working while the timed path is under suspicion, and
    turns each one into a controlled pair: same tee time, same day, same code,
    minutes apart, differing only in the wait. A timed refusal that clears on
    the untimed retry is the comparison that says what the club's "blocked by
    another user" actually means.

    Only results known to have reserved nothing are retried, so a Reserve whose
    outcome could not be established is never sent twice.
  EOT
  type        = bool
  default     = true
}

variable "walden_measure_clock_skew" {
  description = <<-EOT
    Probe the club's clock while staging, and send the Reserve early enough to
    *arrive* as the booking window opens rather than to leave then.

    Two things sit between us and the window and both run against us: the club's
    clock reaches 06:30:00 before ours does, and the request still has to fly
    there. Firing at our own 06:30:00.000 has been landing something like half a
    second into a window members have been clicking into since it opened.

    On. The lead is measured rather than assumed and clamped in the booker, and
    a failed measurement sends unled, so the downside is bounded at the old
    behaviour. Timed bookings only. Turn off to go back to firing at our clock.
  EOT
  type        = bool
  default     = true
}

variable "walden_reserve_sweep_offsets_ms" {
  description = <<-EOT
    Milliseconds past 06:30:00 to ask for the target slot at, comma-separated,
    before any fallback tee time is tried.

    The club refuses for roughly the first second past the window, wording every
    refusal "This slot is blocked by another user". On 2026-08-08 a byte-identical
    request - same slot, same component id, same ViewState - was refused at 0ms,
    refused at 812ms and accepted at 1291ms. 2026-08-12 repeated the pattern at
    0/817/1239ms, on a 5:00 PM slot no other member wanted: the whole sheet was
    still open an hour later. Asking once on the instant is therefore the single
    worst-timed question we can ask.

    Each rung doubles as a measurement - the race ledger records which offsets
    were refused and which was granted - so a morning narrows the boundary to the
    rung spacing whether or not it wins. Once it is known, collapse this to one
    well-chosen offset.

    "0" restores the historical single shot on the instant.
  EOT
  type        = string
  default     = "0,150,300,500,750,1000,1250,1500,1750"
}

variable "walden_capture_race_ledger" {
  description = <<-EOT
    Write the per-attempt race ledger to the debug artifacts bucket.

    Only the final Reserve response was ever kept, and on both mornings the club
    actually granted a slot the evidence sat in an earlier one. This stores every
    attempt's raw partial-response, including the <eval> scripts and callback
    parameters the parser used to discard - the place a dialog the club *showed*
    is expected to differ from one it merely re-rendered.

    On. It writes only on the race path and only to the debug bucket.
  EOT
  type        = bool
  default     = true
}

variable "walden_fast_booking_batch" {
  description = <<-EOT
    Whether the scheduled 6:30 batch runs the fast chain (JS, or direct HTTP
    when the flag above is on) instead of the original Selenium flow. This is
    what the fast chain was built for, so it defaults on.

    Turning it off is a genuine kill switch, not just a speed change: the batch
    falls back to a Python-side precision wait for the booking window, because
    the 6:30 gate itself lives inside the fast chain.
  EOT
  type        = bool
  default     = true
}

variable "walden_fast_booking_immediate" {
  description = <<-EOT
    Whether an ad-hoc booking - a date already inside the 7-day window, booked
    on the spot rather than at 6:30 - runs the fast chain instead of the
    original Selenium flow.

    On so the fast/direct chain can be exercised off-race, on a tee time where
    losing the slot costs nothing, instead of first meeting real traffic during
    a live 6:30 race. See issue #124.
  EOT
  type        = bool
  default     = true
}

variable "debug_artifacts_bucket" {
  description = "GCS bucket name for debug artifacts (screenshots + HTML)"
  type        = string
  default     = "gen-lang-client-0822973627-teetime-debug-artifacts"
}
