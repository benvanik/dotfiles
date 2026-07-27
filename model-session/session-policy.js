const OUTER_SESSION_INSTRUCTION =
  "Session boundaries are owned by the outer profile wrapper. " +
  "Exit Pi, then run ./pi for a new session or ./pi resume to resume one.";

function cancelSessionChange(ctx, command) {
  ctx.ui.notify(
    `/${command} is disabled in this isolated session. ${OUTER_SESSION_INSTRUCTION}`,
    "warning",
  );
  return { cancel: true };
}

export default function sessionPolicy(pi) {
  pi.on("project_trust", (event) => {
    return { trusted: event.cwd === "/workspace" ? "yes" : "no" };
  });

  pi.on("session_before_switch", (event, ctx) => {
    return cancelSessionChange(ctx, event.reason);
  });

  pi.on("session_before_fork", (event, ctx) => {
    const command = event.position === "before" ? "fork" : "clone";
    return cancelSessionChange(ctx, command);
  });
}
