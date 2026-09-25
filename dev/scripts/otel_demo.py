"""Custom script used by the RQ e2e check: logs at every level, optionally raises."""

from extras.scripts import BooleanVar, Script, StringVar


class OtelDemo(Script):
    class Meta:
        name = "OTel demo"
        description = "Logs one line per level for the OpenTelemetry e2e check"
        commit_default = False

    marker = StringVar(description="Text included in every log line")
    fail = BooleanVar(default=False, description="Raise after logging")

    def run(self, data, commit):
        marker = data["marker"]
        self.log_debug(f"{marker} debug")
        self.log_info(f"{marker} info")
        self.log_success(f"{marker} success")
        self.log_warning(f"{marker} warning")
        self.log_failure(f"{marker} failure")
        if data["fail"]:
            raise RuntimeError(f"{marker} boom")
        return marker
