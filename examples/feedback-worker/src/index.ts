export interface Env {
  GITHUB_TOKEN: string;
  GITHUB_REPO: string;
  GITHUB_LABELS?: string;
  GITHUB_ASSIGNEES?: string;
  BREVO_API_KEY?: string;
  RESEND_API_KEY?: string;
  POSTMARK_SERVER_TOKEN?: string;
  SUPPORT_EMAIL?: string;
  FROM_EMAIL?: string;
  FEEDBACK_SHARED_SECRET?: string;
}

type RecentError = {
  timestamp?: number;
  site?: string;
  exc_class?: string;
  message?: string;
  tool?: string | null;
};

type FeedbackPayload = {
  schema_version?: number;
  session_id?: string;
  type?: string;
  content?: string;
  version?: string;
  os?: string;
  model?: string;
  recent_errors?: RecentError[];
  session?: Record<string, unknown>;
  client?: Record<string, unknown>;
  repo?: Record<string, unknown>;
  context?: {
    recent_errors?: RecentError[];
    last_messages?: unknown[];
    tool_calls?: unknown[];
    subagents?: unknown[];
  };
  privacy?: Record<string, unknown>;
};

type GitHubIssue = {
  number?: number;
  html_url?: string;
};

const MAX_CONTENT_LENGTH = 10_000;
const MAX_RECENT_ERRORS = 10;
const MAX_CONTEXT_ITEMS = 20;

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method === "OPTIONS") {
      return corsResponse(null, 204);
    }
    if (request.method !== "POST") {
      return jsonResponse({ error: "method_not_allowed" }, 405);
    }

    if (env.FEEDBACK_SHARED_SECRET) {
      const auth = request.headers.get("authorization") || "";
      if (auth !== `Bearer ${env.FEEDBACK_SHARED_SECRET}`) {
        return jsonResponse({ error: "unauthorized" }, 401);
      }
    }

    let payload: FeedbackPayload;
    try {
      payload = (await request.json()) as FeedbackPayload;
    } catch {
      return jsonResponse({ error: "invalid_json" }, 400);
    }

    const content = (payload.content || "").trim();
    const recentErrors = Array.isArray(payload.context?.recent_errors)
      ? payload.context.recent_errors.slice(0, MAX_RECENT_ERRORS)
      : Array.isArray(payload.recent_errors)
        ? payload.recent_errors.slice(0, MAX_RECENT_ERRORS)
        : [];
    if (!content && recentErrors.length === 0) {
      return jsonResponse({ error: "empty_feedback" }, 400);
    }
    if (content.length > MAX_CONTENT_LENGTH) {
      return jsonResponse({ error: "feedback_too_large" }, 413);
    }

    const sanitizedPayload: FeedbackPayload = {
      session_id: trim(payload.session_id, 128),
      type: trim(payload.type || "feedback", 32),
      content,
      version: trim(payload.version, 64),
      os: trim(payload.os, 128),
      model: trim(payload.model, 128),
      recent_errors: recentErrors.map(sanitizeRecentError),
      session: sanitizeRecord(payload.session),
      client: sanitizeRecord(payload.client),
      repo: sanitizeRecord(payload.repo, 24, 20_000),
      context: {
        recent_errors: recentErrors.map(sanitizeRecentError),
        last_messages: sanitizeArray(payload.context?.last_messages),
        tool_calls: sanitizeArray(payload.context?.tool_calls),
        subagents: sanitizeArray(payload.context?.subagents),
      },
      privacy: sanitizeRecord(payload.privacy),
    };

    const title = githubTitle(sanitizedPayload);
    const body = githubBody(sanitizedPayload, request);

    const issue = await createGithubIssue(env, sanitizedPayload, title, body);
    await sendSupportEmail(env, title, body);

    return jsonResponse({ number: issue.number, html_url: issue.html_url }, 201);
  },
};

function trim(value: unknown, maxLength: number): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  return trimmed.slice(0, maxLength);
}

function sanitizeRecentError(error: RecentError): RecentError {
  return {
    timestamp: typeof error.timestamp === "number" ? error.timestamp : undefined,
    site: trim(error.site, 128),
    exc_class: trim(error.exc_class, 128),
    message: trim(error.message, 500),
    tool: trim(error.tool, 128) || null,
  };
}

function sanitizeValue(value: unknown, maxStringLength = 2_000): unknown {
  if (typeof value === "string") return value.slice(0, maxStringLength);
  if (typeof value === "number" || typeof value === "boolean" || value === null) return value;
  if (Array.isArray(value)) return value.slice(0, MAX_CONTEXT_ITEMS).map((item) => sanitizeValue(item));
  if (typeof value === "object" && value !== null) return sanitizeRecord(value as Record<string, unknown>);
  return undefined;
}

function sanitizeRecord(
  value: unknown,
  maxKeys = 20,
  maxStringLength = 2_000,
): Record<string, unknown> | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const out: Record<string, unknown> = {};
  for (const [key, raw] of Object.entries(value).slice(0, maxKeys)) {
    out[key.slice(0, 80)] = sanitizeValue(raw, maxStringLength);
  }
  return out;
}

function sanitizeArray(value: unknown): unknown[] | undefined {
  return Array.isArray(value)
    ? value.slice(0, MAX_CONTEXT_ITEMS).map((item) => sanitizeValue(item))
    : undefined;
}

function githubTitle(payload: FeedbackPayload): string {
  const prefix = payload.type === "error" ? "Error report" : "Feedback";
  const version = payload.version ? ` ${payload.version}` : "";
  const suffix = payload.session_id ? ` (${payload.session_id.slice(0, 8)})` : "";
  return `[Pythinker CLI] ${prefix}${version}${suffix}`;
}

function githubBody(payload: FeedbackPayload, request: Request): string {
  const lines = [
    "## User submission",
    "",
    payload.content || "_(no comment)_",
    "",
    "## Context",
    "",
    `- Type: ${payload.type || "feedback"}`,
    `- Session: ${payload.session_id || "unknown"}`,
    `- Version: ${payload.version || "unknown"}`,
    `- OS: ${payload.os || "unknown"}`,
    `- Model: ${payload.model || "unknown"}`,
    `- Received at: ${new Date().toISOString()}`,
    `- CF ray: ${request.headers.get("cf-ray") || "unknown"}`,
  ];

  appendJsonSection(lines, "Privacy", payload.privacy);
  appendJsonSection(lines, "Repository", payload.repo);

  if (payload.recent_errors?.length) {
    lines.push("", "## Recent errors", "");
    for (const error of payload.recent_errors) {
      lines.push(
        `- ${error.site || "unknown"}: ${error.exc_class || "unknown"}` +
          `${error.tool ? ` (tool=${error.tool})` : ""}` +
          `${error.message ? ` — ${error.message}` : ""}`,
      );
    }
  }

  appendJsonSection(lines, "Recent visible messages", payload.context?.last_messages);
  appendJsonSection(lines, "Tool calls", payload.context?.tool_calls);
  appendJsonSection(lines, "Subagents", payload.context?.subagents);

  return lines.join("\n");
}

function appendJsonSection(lines: string[], title: string, value: unknown): void {
  if (value === undefined || value === null) return;
  if (Array.isArray(value) && value.length === 0) return;
  if (typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === 0) return;
  lines.push("", `## ${title}`, "", "```json", JSON.stringify(value, null, 2), "```");
}

async function createGithubIssue(
  env: Env,
  payload: FeedbackPayload,
  title: string,
  body: string,
): Promise<GitHubIssue> {
  const labels = unique([
    ...splitCsv(env.GITHUB_LABELS || "feedback,pythinker-cli"),
    `feedback:${payload.type || "feedback"}`,
  ]);
  const assignees = splitCsv(env.GITHUB_ASSIGNEES || "");
  const response = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/issues`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "pythinker-feedback-worker",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ title, body, labels, assignees }),
  });

  if (!response.ok) {
    throw new Error(`GitHub issue creation failed: ${response.status}`);
  }
  const issue = (await response.json()) as GitHubIssue;
  return { number: issue.number, html_url: issue.html_url };
}

async function sendSupportEmail(env: Env, subject: string, body: string): Promise<void> {
  const to = env.SUPPORT_EMAIL || "support@pythinker.com";
  const from = env.FROM_EMAIL || "Pythinker Feedback <feedback@pythinker.com>";

  if (env.BREVO_API_KEY) {
    const sender = parseMailbox(from);
    const response = await fetch("https://api.brevo.com/v3/smtp/email", {
      method: "POST",
      headers: {
        "api-key": env.BREVO_API_KEY,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        sender,
        to: [{ email: to }],
        subject,
        textContent: body,
      }),
    });
    if (!response.ok) {
      throw new Error(`Brevo email failed: ${response.status}`);
    }
    return;
  }

  if (env.RESEND_API_KEY) {
    const response = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.RESEND_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ from, to, subject, text: body }),
    });
    if (!response.ok) {
      throw new Error(`Resend email failed: ${response.status}`);
    }
    return;
  }

  if (env.POSTMARK_SERVER_TOKEN) {
    const response = await fetch("https://api.postmarkapp.com/email", {
      method: "POST",
      headers: {
        "X-Postmark-Server-Token": env.POSTMARK_SERVER_TOKEN,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ From: from, To: to, Subject: subject, TextBody: body }),
    });
    if (!response.ok) {
      throw new Error(`Postmark email failed: ${response.status}`);
    }
  }
}

function parseMailbox(value: string): { email: string; name?: string } {
  // Linear parse (no backtracking regex) for the "Name <addr>" form to avoid
  // polynomial ReDoS on uncontrolled header input.
  const trimmed = value.trim();
  const lt = trimmed.indexOf("<");
  if (lt === -1 || !trimmed.endsWith(">")) return { email: trimmed };
  const email = trimmed.slice(lt + 1, -1).trim();
  const name = trimmed.slice(0, lt).trim().replace(/^"|"$/g, "");
  return { name, email };
}

function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function unique(values: string[]): string[] {
  return Array.from(new Set(values));
}

function corsResponse(body: BodyInit | null, status: number): Response {
  return new Response(body, { status, headers: corsHeaders() });
}

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders(), "Content-Type": "application/json" },
  });
}

function corsHeaders(): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
  };
}
