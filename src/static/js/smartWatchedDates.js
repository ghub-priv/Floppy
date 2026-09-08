// Smart Watched Dates v1.0.3 - Git-native picker integration.
//
// The old runtime patch injected regional release metadata into the movie
// details payload. This version keeps that metadata behind a dedicated JSON
// endpoint and resolves it only for TMDB movie tracking forms.
(function () {
  if (window.__floppySmartWatchedDatesRegistered) {
    return;
  }
  window.__floppySmartWatchedDatesRegistered = true;
  window.__floppySmartWatchedDatesRequests =
    window.__floppySmartWatchedDatesRequests || new Map();

  function register() {
    if (!window.Alpine || window.__floppySmartWatchedDatesAlpineData) {
      return;
    }
    window.__floppySmartWatchedDatesAlpineData = true;

    Alpine.data("smartWatchedDates", (config) => ({
      endpoint: config.endpoint || "",
      suggestions: [],
      loading: false,
      loaded: false,

      init() {
        this.load();
      },

      mediaIdentity() {
        const form = this.$root.closest("form");
        if (!form) {
          return null;
        }
        const value = (name) =>
          String(form.querySelector(`[name="${name}"]`)?.value || "").trim();
        return {
          source: value("source"),
          mediaType: value("media_type"),
          mediaId: value("media_id"),
        };
      },

      async load() {
        if (this.loaded || this.loading || !this.endpoint) {
          return;
        }

        const identity = this.mediaIdentity();
        if (
          !identity ||
          identity.source !== "tmdb" ||
          identity.mediaType !== "movie" ||
          !identity.mediaId
        ) {
          this.loaded = true;
          return;
        }

        this.loading = true;
        const url = new URL(this.endpoint, window.location.origin);
        url.searchParams.set("source", identity.source);
        url.searchParams.set("media_type", identity.mediaType);
        url.searchParams.set("media_id", identity.mediaId);
        const requestKey = url.toString();

        let request = window.__floppySmartWatchedDatesRequests.get(requestKey);
        if (!request) {
          request = fetch(requestKey, {
            credentials: "same-origin",
            headers: { Accept: "application/json" },
          }).then(async (response) => {
            if (!response.ok) {
              throw new Error(`Smart Watched Dates HTTP ${response.status}`);
            }
            return response.json();
          });
          window.__floppySmartWatchedDatesRequests.set(requestKey, request);
        }

        try {
          const payload = await request;
          this.suggestions = Array.isArray(payload?.suggestions)
            ? payload.suggestions.filter(
                (suggestion) => suggestion && suggestion.date,
              )
            : [];
        } catch (error) {
          // Suggestions are an enhancement. Provider/network failures must not
          // prevent ordinary manual date tracking.
          console.warn("Smart Watched Dates unavailable", error);
          this.suggestions = [];
        } finally {
          // The map exists only to collapse the simultaneous Start/End picker
          // fetches. Do not retain a region-specific response across later
          // modal opens, because the user's preferred region may have changed.
          if (window.__floppySmartWatchedDatesRequests.get(requestKey) === request) {
            window.__floppySmartWatchedDatesRequests.delete(requestKey);
          }
          this.loaded = true;
          this.loading = false;
        }
      },

      formatDate(iso) {
        if (!iso) {
          return "";
        }
        const [year, month, day] = iso.slice(0, 10).split("-").map(Number);
        if ([year, month, day].some(Number.isNaN)) {
          return iso;
        }
        try {
          return new Intl.DateTimeFormat(
            document.documentElement.lang || undefined,
            { day: "numeric", month: "short", year: "numeric" },
          ).format(new Date(year, month - 1, day));
        } catch (_error) {
          return iso;
        }
      },

      apply(suggestion) {
        const iso = suggestion?.date || "";
        if (!iso) {
          return;
        }

        const pickerRoot = this.$root.closest("[data-date-time-picker-root]");
        if (!pickerRoot || !window.Alpine) {
          return;
        }

        let picker;
        try {
          picker = Alpine.$data(pickerRoot);
        } catch (_error) {
          return;
        }
        if (!picker?.commit || !picker?.formatValueFromParts) {
          return;
        }

        const [year, month, day] = iso.slice(0, 10).split("-").map(Number);
        if ([year, month, day].some(Number.isNaN)) {
          return;
        }

        // Preserve the accepted v1.0.2/v1.0.3 behaviour: release-date
        // suggestions represent a date, not the current clock time. When
        // Floppy tracks time, use noon to avoid midnight/timezone edge cases.
        const hour = picker.trackTime ? 12 : 0;
        picker.commit(
          picker.formatValueFromParts(year, month, day, hour, 0, 0),
        );
        // Existing paired-field/runtime behaviour remains owned by the core
        // picker. Smart Watched Dates only invokes the established start-date
        // backfill hook after applying its date.
        picker.backfillStartDateIfNeeded?.();
        picker.closePicker?.();
      },
    }));
  }

  if (window.Alpine) {
    register();
  } else {
    document.addEventListener("alpine:init", register, { once: true });
  }
})();
