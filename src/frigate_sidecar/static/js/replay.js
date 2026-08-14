// Push replay workbench.
(function () {
  var btn = document.getElementById("run-btn");
  var stateEl = document.getElementById("run-state");
  var output = document.getElementById("output");

  btn.addEventListener("click", async function () {
    btn.disabled = true;
    output.textContent = "";
    stateEl.textContent = "running...";

    var scenario = document.getElementById("scenario").value;
    var speed = parseFloat(
      document.querySelector('input[name="speed"]:checked').value
    );
    var dryRun = document.getElementById("dry-run").checked;
    var stacked = document.getElementById("stacked").checked;

    var scenarios = [scenario];
    var stagger = 8;
    if (stacked) {
      scenarios.push(document.getElementById("scenario2").value);
      stagger = parseFloat(document.getElementById("stagger").value) || 8;
    }

    try {
      var resp = await fetch("/replay/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scenarios: scenarios,
          speed: speed,
          dry_run: dryRun,
          stagger: stagger,
        }),
      });

      // Parse text first: a non-JSON body (proxy error page, HTML 500 from
      // an older sidecar) must surface as a readable error, not a
      // SyntaxError swallowing the real cause.
      var raw = await resp.text();
      var run;
      try {
        run = JSON.parse(raw);
      } catch (parseErr) {
        stateEl.textContent =
          "error: HTTP " + resp.status + " — " + raw.slice(0, 200);
        btn.disabled = false;
        return;
      }

      if (!resp.ok) {
        stateEl.textContent = "error: " + (run.detail || resp.status);
        btn.disabled = false;
        return;
      }

      stateEl.textContent =
        run.state + " (" + run.messages_sent + "/" + run.messages_total + ")";

      if (run.decisions && run.decisions.length > 0) {
        output.textContent = run.decisions
          .map(function (d) {
            var parts = [
              "step " +
                d.step +
                ": " +
                d.topic.split("/").pop() +
                "/" +
                d.type,
              "mutation=" + d.mutation,
            ];
            if (d.level) parts.push("level=" + d.level);
            if (d.sounded !== undefined)
              parts.push("sounded=" + (d.sounded ? "yes" : "no"));
            if (d.sound_name) parts.push("sound=" + d.sound_name);
            if (d.interruption_level)
              parts.push("interruption=" + d.interruption_level);
            if (d.la_action) {
              var la = "LA=" + d.la_action;
              if (d.la_token_type) la += " (" + d.la_token_type + ")";
              parts.push(la);
            }
            return parts.join("  ");
          })
          .join("\n");
      } else {
        output.textContent = run.messages_sent + " messages published to MQTT";
      }

      if (run.error) {
        output.textContent += "\nerror: " + run.error;
      }
    } catch (e) {
      stateEl.textContent = "error: " + e;
    }
    btn.disabled = false;
  });
})();
