"""Shared security-review knowledge for prompts and advisor context.

Most framework highlights and slug notes are ported from the TypeScript
Pythinker Review security-scan prompt tables. Keep entries short: these are
reviewer instincts and false-positive checks, not tutorials.
"""

from __future__ import annotations

TechHighlight = tuple[str, tuple[str, ...], tuple[str, ...]]

TECH_HIGHLIGHTS: dict[str, TechHighlight] = {
    "actix": (
        "Actix-web",
        ("rust",),
        (
            "Middleware via `App::new().wrap(...)` is global; per-scope wraps via "
            "`web::scope().wrap()` — flag scopes with skipped wraps",
            "Extractors `web::Query<T>` / `web::Json<T>` / `web::Path<T>` are user input — types "
            "only validate STRUCTURE, not content",
            "Auth middleware that returns `next.call(req)` unconditionally before the check is the "
            "bypass shape",
            '`HttpResponse::Ok().body(format!("<html>{}</html>", x))` is XSS — use a templating '
            "crate with escape",
        ),
    ),
    "aiohttp": (
        "aiohttp",
        ("python",),
        (
            "Middleware via `@web.middleware` runs in declaration order — auth before logging is "
            "the safe layout",
            "`request.query` / `request.json()` / `request.match_info` / `request.read()` are "
            "user input",
            "`aiohttp_session` cookies need an explicit storage backend with secret rotation; "
            "default `EncryptedCookieStorage` is fine",
            "ClientSession (outbound) — flag user-controlled URLs without an allowlist (SSRF)",
        ),
    ),
    "airflow": (
        "Airflow",
        ("python",),
        (
            "DAGs run with the Airflow scheduler's privileges — operator template fields (`{{ "
            "params.x }}`) interpolated into Bash/SQL/HTTP are injection sinks",
            '`BashOperator(bash_command=f"... {x}")` is shell injection — even non-templated '
            "f-strings are risky if x is user-influenced",
            "Connections and Variables hold credentials — leaking them via XCom or logs is data "
            "exposure",
            "REST API auth (`auth_backends`) — defaults can be permissive on older versions",
        ),
    ),
    "android": (
        "Android",
        (),
        (
            '`android:exported="true"` on Activity/Service/Receiver/Provider exposes the '
            "component to other apps — confirm with intent + permission",
            "Implicit `<intent-filter>` makes a component exported on pre-API-31 even without "
            '`android:exported="true"` — flag legacy code',
            'Deeplink schemes (`<data android:scheme="..."/>`) — review URL handling for SSRF '
            "(WebView), file:// loads, JS bridges",
            "`WebView` with `setJavaScriptEnabled(true)` + `addJavascriptInterface` is RCE if "
            "the loaded URL is attacker-controlled",
            "ContentProvider exported without permission grants reads/writes to any app — "
            '`android:grantUriPermissions="true"` widens scope further',
        ),
    ),
    "apex": (
        "Apex (Salesforce)",
        (),
        (
            "`without sharing` classes BYPASS row-level security — confirm every `without sharing` "
            "is intentional and that the methods can't be invoked by unprivileged users",
            "`@AuraEnabled` methods are reachable from Lightning components without extra auth — "
            "same surface as REST",
            "`Database.query('SELECT ... WHERE ... = \\'' + userInput + '\\'')` is SOQL injection; "
            "`[SELECT ... WHERE id = :userInput]` is bound and safe",
            "FLS / CRUD checks (`Schema.sObjectType.X.isAccessible()`) are NOT automatic — flag DML "
            "on sObjects without explicit checks",
            "`@RestResource(urlMapping='...')` exposes the class on `/services/apexrest/` — public "
            "to authenticated Salesforce users; confirm the data filter",
        ),
    ),
    "astro": (
        "Astro",
        ("typescript", "javascript"),
        (
            "`pages/api/**/*.ts` exports (`GET`/`POST`/...) are public; `prerender = false` opts a "
            "page into SSR with the same auth concerns",
            "`Astro.request` / `Astro.cookies` / `Astro.params` are user input — same sinks as "
            "Next.js",
            "Default output is static; double-check if a route silently became SSR via `export "
            "const prerender = false`",
            "Astro uses Vite — env vars prefixed with `PUBLIC_` ship to the client bundle",
        ),
    ),
    "aws-lambda": (
        "AWS Lambda",
        (),
        (
            "API Gateway authorizer claims live on `event.requestContext.authorizer` — "
            "handlers that don't read them are unauthenticated",
            "`event.body` is JSON-string in proxy integrations — JSON.parse failures should "
            "NOT echo `event` (leaks request data into logs)",
            "IAM role on the function determines blast radius — over-permissioned roles + RCE "
            "= account takeover",
            "Cold-start global state is shared across invocations on the same container — "
            "credentials/PII can leak between tenants",
            "Lambda timeouts default to 3s but can be 15min — long-running handlers without "
            "per-call rate limits enable cost amplification",
        ),
    ),
    "axum": (
        "Axum",
        ("rust",),
        (
            '`Router::new().route("/", get(h)).layer(auth_layer)` — `.layer` order matters; routes '
            "added AFTER `.layer` may not be wrapped",
            "`Extension<User>` / `State<App>` carry auth identity — flag handlers that skip them",
            "`Path<T>` / `Query<T>` / `Json<T>` extractors are user input; same sinks as Actix",
            "`.merge(other_router)` and `.nest(prefix, other)` — sub-routers inherit parent layers "
            "but the order of `.layer` vs `.merge`/`.nest` matters",
        ),
    ),
    "azure-functions": (
        "Azure Functions",
        ("csharp", "javascript", "typescript", "python"),
        (
            "`AuthorizationLevel.Anonymous` on `HttpTrigger` is a public endpoint — "
            "confirm intent; `Function`/`Admin` require a function key",
            "Function keys are NOT user identity — they authenticate the *caller app*, "
            "not a user; for user auth use Easy Auth or App Service Authentication",
            "Triggers (Queue/ServiceBus/Blob) are reached via Azure infra — payloads are "
            "still user input if any web caller can write to the queue",
            'Bindings (e.g. `[Blob("path/{queueTrigger}")]`) interpolate input into '
            "resource paths — can be path traversal across containers",
        ),
    ),
    "bottle": (
        "Bottle",
        ("python",),
        (
            "Bottle has no built-in auth — every `@route` is public unless a decorator chain "
            "enforces a check",
            "`request.query` / `request.forms` / `request.json` are user input",
            "SimpleTemplate `{{!x}}` is unescaped; `{{x}}` auto-escapes — flag the bang form",
            "`static_file(filename, root)` without `path.basename(filename)` is path traversal",
        ),
    ),
    "buffalo": (
        "Buffalo",
        ("go",),
        (
            "`app.Use(...)` middleware is global; `app.Resource(...)` registers CRUD — confirm "
            "auth wraps both",
            "`c.Param('x')` / `c.Request()` / `c.Bind(&v)` are user input",
            "`render.Auto` chooses HTML / JSON / XML by Accept header — DB rows in the response "
            "include all columns; use a response shape",
        ),
    ),
    "bullmq": (
        "BullMQ",
        ("typescript", "javascript"),
        (
            "`job.data` is whatever the producer enqueued — treat it as user input if any web "
            "handler can enqueue",
            "Workers run with elevated trust (no auth context) — confirm the queue boundary "
            "validates / authorizes the request before enqueue",
            "Retry on poison messages can amplify a single attacker payload across retries — flag "
            "handlers without idempotency keys",
            "`Queue.add(..., { delay })` at long delays plus user-controlled payload = "
            "stored-XSS-via-job",
        ),
    ),
    "bun": (
        "Bun",
        ("typescript", "javascript"),
        (
            "`Bun.serve({ fetch })` is a raw HTTP entry — auth/validation lives entirely in the "
            "handler, no framework gates",
            "`Bun.spawn(...)` / `Bun.$`...`` shell template — interpolated user input is RCE-shaped",
            "Bun's TLS/HTTP defaults differ from Node; verify rejected-cert handling on outbound "
            "`fetch`",
        ),
    ),
    "cakephp": (
        "CakePHP",
        ("php",),
        (
            "`$this->Auth->allow(...)` opens specific actions to the public — confirm the list "
            "is intentional",
            "`$this->request->getData()` is user input; mass assignment via `patchEntity()` "
            "without `accessibleFields` is the bug",
            "Bake-generated views use `h($x)` for escape — flag templates that emit raw `$x` "
            "without `h()`",
            "`->find()->where(['col' => $x])` is parameterized; `->find()->where(\"col = "
            "'$x'\")` is SQLi",
        ),
    ),
    "celery": (
        "Celery",
        ("python",),
        (
            "Task args are deserialized via the configured serializer — `pickle` is unsafe "
            "deserialization (RCE)",
            "Tasks run with worker-level trust (no request user) — re-validate ownership when a "
            "task acts on user data",
            "`task.delay(user_id=...)` invocations from web code: confirm the call site "
            "authenticates the user before enqueue",
            "Long retries on poison messages can amplify a single bad payload",
        ),
    ),
    "chi": (
        "Chi",
        ("go",),
        (
            "`r.Use(middleware)` and `r.Group(...)` define auth scopes — sub-routers inherit, but "
            '`r.Mount("/x", h)` does NOT inherit middleware applied after the mount',
            '`chi.URLParam(r, "id")` is user input; treat as untrusted in DB / fs / exec calls',
            "`render.JSON(w, r, data)` returns whatever you pass — DB rows often include secret "
            "columns; use a response-shape struct",
        ),
    ),
    "clojure": (
        "Clojure (Ring/Compojure)",
        (),
        (
            "Ring middleware composes via `wrap-*`; auth must be in the chain BEFORE the route "
            "handler",
            "`wrap-anti-forgery` (CSRF) is opt-in — flag apps using session cookies without it",
            'Compojure `(GET "/x" [id] ...)` destructures params; `(get-in request [:params '
            ":x])` is the same — both untrusted",
            "`ring.util.response/redirect` to user-controlled paths is open-redirect without an "
            "allowlist",
        ),
    ),
    "cobra": (
        "Cobra",
        ("go",),
        (
            "Privileged CLI surface — flags often hold secrets (`--token`, `--password`); flag any "
            "logging of `cmd.Flags()`",
            "`Run`/`RunE` handlers operate with the operator's privileges; user-supplied args "
            "interpolated into shell or SQL are injection",
            "`PersistentFlags` propagate to subcommands — credential flags on a parent leak to all "
            "children",
        ),
    ),
    "codeigniter": (
        "CodeIgniter",
        ("php",),
        (
            "Filters in `app/Config/Filters.php` are the auth gate; routes outside the "
            "filter scope are public",
            "`$this->request->getVar('x')` / `getPost()` are user input — concatenation into "
            'SQL via `$db->query("...$x...")` is injection',
            "`view('name', $data)` auto-escapes; setting the third arg to disable escape "
            "requires explicit trust review",
            "`helper()` and `service()` calls can load arbitrary code if names are user-influenced",
        ),
    ),
    "dart": (
        "Dart (Shelf)",
        (),
        (
            "Shelf has no built-in auth — `Pipeline().addMiddleware()` is the gate, registration "
            "order matters",
            "`request.url.queryParameters` / `request.readAsString()` are user input",
            "`Response.ok(body)` doesn't HTML-escape; templates need explicit escape if rendering "
            "HTML",
            "`io.serve(handler, ...)` exposes the handler directly — no framework gates beyond what "
            "you write",
        ),
    ),
    "deno": (
        "Deno",
        ("typescript", "javascript"),
        (
            "`Deno.serve(handler)` is the entry; no built-in auth — middleware order is hand-rolled",
            "Permissions (`--allow-net`, `--allow-read`, `--allow-env`) are deploy-time; code that "
            "calls `Deno.permissions.request` at runtime is suspicious",
            "Oak `ctx.request.body()` / `ctx.params` are untrusted; same sinks as Express",
        ),
    ),
    "django": (
        "Django",
        ("python",),
        (
            "`@csrf_exempt` views handling state-changing POSTs without an alternate auth "
            "(signature, token) are CSRF-vulnerable",
            "`Model.objects.raw(...)` / `cursor.execute()` with f-string interpolation is SQL "
            "injection — flag any %-formatted SQL",
            "`mark_safe()` / `format_html()` on user input is XSS; same for `{% autoescape off "
            "%}` blocks",
            "`ModelForm` without `fields = [...]` (or with `__all__`) exposes mass-assignment of "
            "every model column",
            "`DEBUG=True` + `ALLOWED_HOSTS=['*']` in any reachable settings file leaks tracebacks "
            "and SECRET_KEY material",
        ),
    ),
    "djangorestframework": (
        "Django REST Framework",
        ("python",),
        (
            "`permission_classes` missing or set to `AllowAny` on a sensitive "
            "`ModelViewSet` exposes full CRUD",
            "`ModelSerializer` with `fields = '__all__'` allows mass-assignment of "
            "admin-only columns via PATCH",
            "`@action(detail=True)` methods inherit the viewset's permissions but "
            "custom routers can break this — confirm",
        ),
    ),
    "dotnet": (
        ".NET / ASP.NET Core",
        ("csharp",),
        (
            "`[Authorize]` is the gate; `[AllowAnonymous]` on a sensitive action opens it back up "
            "— confirm intent",
            "`[FromQuery]` / `[FromBody]` / `[FromRoute]` are user input — model binding is "
            "structure-only",
            "`[ApiController]` adds automatic 400 on model-state errors; absence means the "
            "handler MUST check `ModelState.IsValid`",
            "Razor `@Html.Raw(x)` on user input is XSS; bare `@x` HTML-encodes (safe)",
            "Minimal API `app.MapGet(...).RequireAuthorization()` is the gate — flag chains "
            "without it on sensitive routes",
            'EF Core `FromSqlRaw($"... {x} ...")` is SQLi; `FromSqlInterpolated($"... {x} ...")` '
            "parameterizes correctly",
        ),
    ),
    "drupal": (
        "Drupal",
        ("php",),
        (
            "`*.routing.yml` `_permission`/`_access` keys are the gate — `access content` is "
            "permissive (most authenticated users have it)",
            "`\\Drupal::request()->query->get('x')` / `request->get('x')` are user input",
            "`$this->t('@name', ['@name' => $userInput])` auto-escapes via `@`/`%`; bare "
            "placeholders without prefix are unsafe",
            "`db_query(\"... $x\")` is SQL injection; `\\Drupal::database()->query('... :x', "
            "[':x' => $x])` is parameterized",
        ),
    ),
    "echo": (
        "Echo",
        ("go",),
        (
            "`e.Use(middleware)` order matters — routes registered before `Use` aren't covered",
            '`c.Bind(&v)` accepts JSON/form/query — fields with `json:"-"` matter only if you USE '
            '`json:"-"`; explicit allowlists in DTO structs are the safe form',
            'Group-level middleware (`g := e.Group("/api", auth)`) — confirm sensitive routes live '
            "under the group, not on the root `e`",
        ),
    ),
    "erlang": (
        "Erlang (Cowboy)",
        (),
        (
            "`init/2` is the cowboy entry — auth check must happen before any state-changing call",
            "`cowboy_req:binding(name, Req)` / `read_body/1` / `parse_qs/1` are user input",
            "Erlang term decoding from external sources via `binary_to_term/1` is unsafe "
            "deserialization — use `binary_to_term/2` with `[safe]`",
            "Process-per-request model isolates handler crashes, but supervision-tree restart "
            "strategies can hide errors",
        ),
    ),
    "express": (
        "Express.js",
        ("typescript", "javascript"),
        (
            "Each `app.get/post/...` and `router.use` is a public endpoint — confirm auth "
            "middleware actually wraps it (order matters; routes mounted before "
            "`app.use(authMiddleware)` are unprotected)",
            "`req.query`/`req.params`/`req.body` are user input; concatenation into SQL, shell, "
            "paths, or URLs is the usual sink",
            "`express.static` on a user-influenced root, or `res.sendFile(req.params.x)`, is "
            "path traversal",
            "Error handlers that send `err.stack` or `err.message` to the response leak internals",
            "CORS `origin: true` reflecting credentials enables CSRF-via-fetch",
        ),
    ),
    "falcon": (
        "Falcon",
        ("python",),
        (
            "`on_<method>(self, req, resp, ...)` handlers are public unless a middleware/hook "
            "checks auth",
            "`req.media` / `req.params` / `req.get_param('x')` are user input",
            "`req.context` carries auth-claim data — confirm it's set BEFORE the resource handler "
            "runs",
            "Falcon's `resp.media` accepts dicts directly; over-fetching DB rows leaks PII",
        ),
    ),
    "fastapi": (
        "FastAPI",
        ("python",),
        (
            "Auth lives in `Depends(...)`; routes without an auth dependency are public — "
            "`@app.get('/admin')` with no Depends is the common gap",
            "Pydantic models validate input but `Optional[Any]` / `dict` fields are an escape "
            "hatch — flag them on inputs",
            "`response_model=...` filters server output; without it, you may return DB columns "
            "containing secrets",
            "`StaticFiles(directory=...)` rooted at a user-influenced path is path traversal",
        ),
    ),
    "fastify": (
        "Fastify",
        ("typescript", "javascript"),
        (
            "`preHandler` / `onRequest` hooks are the auth layer; routes registered without them "
            "or before the auth plugin are unprotected",
            "Schema validation (`schema: { body, querystring }`) is the default mitigation — "
            "flag handlers that read raw `request.body` without a schema",
            "Plugins registered with `register()` inherit hooks per-scope; cross-scope auth "
            "bypass is common in monorepos",
            "`reply.send(err)` returns full error objects in dev mode; check the prod config",
        ),
    ),
    "fiber": (
        "Fiber",
        ("go",),
        (
            "`app.Get/Post/...` registers public endpoints; middleware via `app.Use(auth)` must "
            "precede them, and route-level middleware override group middleware",
            "`c.Query` / `c.Params` / `c.Body` / `c.BodyParser(&v)` are user input; injection "
            "sinks are the same as net/http",
            "Fiber wraps fasthttp — request bodies/headers are not safe to retain past the handler "
            "return; flag goroutines that capture `c` by reference",
        ),
    ),
    "flask": (
        "Flask",
        ("python",),
        (
            "`@app.route(...)` without a `@login_required` (or equivalent) decorator is public; "
            "check the order of decorators — `@app.route` must be outermost",
            "`render_template_string(user_input)` is server-side template injection (RCE)",
            "`request.args` / `request.form` / `request.json` interpolated into SQL via "
            '`db.engine.execute(f"...")` is SQL injection',
            "`send_from_directory(dir, request.args['file'])` without a basename check is path "
            "traversal",
            "`session` cookies use `app.secret_key` — hardcoded keys in source are session forgery",
        ),
    ),
    "gcp-cloud-functions": (
        "GCP Cloud Functions",
        (),
        (
            "Allow-unauthenticated invocations (`--allow-unauthenticated`) make the "
            "function public — confirm via deploy config",
            "IAM-based auth (Cloud IAM) is invoker-level; for user identity, "
            "integrate Identity Platform / Firebase Auth in the handler",
            "Function URLs include the project ID and region — leakage of these is "
            "information disclosure",
            "Background functions (Pub/Sub, Storage triggers) — payload comes from "
            "the GCP infra but is still ATTACKER-INFLUENCED if any web path can "
            "write to the bucket/topic",
        ),
    ),
    "gin": (
        "Gin",
        ("go",),
        (
            "Each `r.GET/POST/...` and `r.Group(...)` is a public endpoint; auth middleware applied "
            "via `r.Use(...)` must precede route registration in the same group",
            "`c.Query`/`c.Param`/`c.PostForm` are user input — usual injection surfaces (SQL, exec, "
            "fs, URL) apply",
            '`c.HTML(http.StatusOK, "tmpl", data)` with `data` containing untrusted strings is XSS '
            "unless the template uses `{{.X}}` (auto-escaped) and not `{{.X | safehtml}}`",
        ),
    ),
    "github-actions": (
        "GitHub Actions",
        ("yaml", "yml"),
        (
            "pull_request_target and workflow_run can expose secrets to untrusted code.",
            "Actions should be pinned; github.event/head_ref interpolation in run scripts "
            "is shell-injection shaped.",
            "permissions: write-all and broad id-token: write need justification.",
        ),
    ),
    "go": (
        "Go web services",
        ("go",),
        (
            "Router middleware must wrap the exact route/group before registration.",
            "c.Query/r.URL.Query/FormValue/path params are untrusted for SQL, exec, fs, "
            "and HTTP clients.",
            "Prefer response-shape structs to raw DB rows.",
        ),
    ),
    "gorilla": (
        "Gorilla mux",
        ("go",),
        (
            "`router.Use(authMiddleware)` covers the router; subrouters via `Subrouter()` "
            "inherit, but `PathPrefix(...).Handler(other)` does not",
            "`mux.Vars(r)` is user input — usual injection sinks (SQL, exec, fs, URL)",
            '`router.HandleFunc("/x", h).Methods("GET")` — flag handlers without an explicit '
            "`.Methods` (accept any verb)",
        ),
    ),
    "grape": (
        "Grape",
        ("ruby",),
        (
            "Auth lives in `before do ... end` or `helpers do ... end` — endpoints without it are "
            "public",
            "`params` is the only safe input accessor; raw `request.body.read` skips Grape's "
            "coercion",
            "`declared(params, include_missing: false)` is strong-params equivalent — flag "
            "handlers that use `params` directly for mass assignment",
            "API versioning paths (`version 'v1'`) — confirm deprecated versions still enforce "
            "auth",
        ),
    ),
    "graphql": (
        "GraphQL",
        ("typescript", "javascript"),
        (
            "Per-resolver auth: every Query/Mutation/Subscription field is independently "
            "reachable — flag resolvers that don't check `context.user`",
            "Field-level vs object-level auth: returning a User object grants access to all "
            "fields unless guards exist on `email`/`role`/etc.",
            "Disabled introspection in prod — leaving it on leaks the full schema (informational "
            "severity)",
            "Query depth/complexity limits stop abusive nested queries; absence is the bug",
            "Aliasing + batching can multiply the cost of an unauthenticated query — expensive "
            "resolvers need rate limits",
        ),
    ),
    "hanami": (
        "Hanami",
        ("ruby",),
        (
            "Each `Hanami::Action` subclass is publicly addressable via the router — `before` "
            "callbacks are the auth gate",
            "Strong-params equivalent: `params.valid?` + a Contract — handlers using raw `params` "
            "skip validation",
            "`include Deps[...]` for DI: shared DB / repo objects can leak ownership semantics if "
            "used as singletons",
        ),
    ),
    "hapi": (
        "Hapi",
        ("typescript", "javascript"),
        (
            "`auth: false` on a `server.route(...)` opts out of the default auth strategy — confirm "
            "it's intentional, especially on writes",
            "Validate routes use `validate: { query, payload, params }`; routes without validation "
            "pass raw input to handlers",
            "`server.auth.default(...)` sets the global gate; flag handlers that pre-date it or "
            "that pass `auth: 'optional'`",
            "`request.payload` / `request.query` / `request.params` are user input",
        ),
    ),
    "hono": (
        "Hono",
        ("typescript", "javascript"),
        (
            "Each `app.get('/path', handler)` is a public endpoint; auth middleware (`app.use('*', "
            "auth)`) must come BEFORE the route declarations or it's a no-op",
            "`c.req.query()` / `c.req.param()` / `c.req.json()` are user input — usual injection "
            "surfaces apply",
            "Hono runs on Workers/edge runtimes — check whether route handlers reach into a "
            "separate Node backend without re-authenticating",
        ),
    ),
    "ios": (
        "iOS",
        (),
        (
            "`CFBundleURLSchemes` registers your app as a URL handler — `application(_:open:)` / "
            "`scene(_:openURLContexts:)` receive attacker-controlled URLs",
            "Universal Links via `apple-app-site-association` — host association determines which "
            "domains can open the app; misconfig is account-takeover-shaped",
            "WKWebView with `loadHTMLString(html, baseURL:)` and a `file://` baseURL gives the page "
            "access to local files",
            "Keychain access without `kSecAttrAccessibleWhenUnlocked` (or stricter) leaks "
            "credentials at app launch",
            "App Transport Security exceptions in Info.plist (`NSAllowsArbitraryLoads`) downgrade "
            "TLS — flag any plist that opts out",
        ),
    ),
    "jaxrs": (
        "JAX-RS (Jersey/Quarkus/RESTEasy)",
        ("java", "kotlin"),
        (
            "`@RolesAllowed`/`@DenyAll`/`@PermitAll` are the gate; absence on a `@Path` resource "
            "is public",
            "`@QueryParam`/`@PathParam`/`@FormParam`/`@HeaderParam` are user input",
            "`@RequestScoped` provider classes can leak per-request state if held by "
            "`@ApplicationScoped` resources",
            "`Response.ok(entity)` with raw JPA entities over-fetches columns; use a DTO",
        ),
    ),
    "kemal": (
        "Crystal Kemal",
        (),
        (
            "No built-in auth — every `get '/path' do ... end` is public unless a `before_*` "
            "filter intercepts",
            '`env.params.url["x"]` / `env.params.json["x"]` / `env.params.body["x"]` are user '
            "input",
            "Crystal's macro-driven JSON parsing is type-safe but content-unvalidated; bounds "
            "checks on collections matter",
        ),
    ),
    "koa": (
        "Koa",
        ("typescript", "javascript"),
        (
            "`router.<verb>` routes registered before `app.use(authMiddleware)` are unprotected — "
            "middleware order matters",
            "`ctx.request.body` / `ctx.query` / `ctx.params` are user input; same injection sinks as "
            "Express",
            "`ctx.throw(401)` is a soft response — confirm it's reached BEFORE any data is "
            "fetched/returned",
            "`koa-bodyparser` defaults to forms+json; large payload limits and prototype-pollution "
            "opts must be set explicitly",
        ),
    ),
    "ktor": (
        "Ktor",
        ("kotlin",),
        (
            '`authenticate("jwt") { ... }` blocks are the gate — routes outside them are public',
            "`call.receive<T>()` deserializes user input — `kotlinx.serialization` is "
            "structure-validating, not content-validating",
            "`call.parameters` / `call.request.queryParameters` are user input",
            "Status pages plugin handles errors — confirm prod config doesn't echo exceptions to "
            "the response",
        ),
    ),
    "lambda-rs": (
        "Rust AWS Lambda",
        ("rust",),
        (
            "`LambdaEvent<T>::payload` is API Gateway / SQS / etc. payload — type-driven but "
            "content is user-supplied",
            "`event.payload.request_context.authorizer` carries claims when API Gateway "
            "authorizer is configured — handler must verify",
            "Cold-start global state (lazy_static / OnceCell) survives across invocations — "
            "credentials/state leakage between tenants",
        ),
    ),
    "laravel": (
        "Laravel",
        ("php",),
        (
            "`Model::create($request->all())` without `$fillable`/`$guarded` is mass assignment "
            "— admin columns get overwritten",
            "`DB::raw()` / `whereRaw()` / `selectRaw()` with interpolated input is SQL injection",
            "`VerifyCsrfToken::$except` lists that include state-changing routes are "
            "CSRF-vulnerable unless an alternate verification (signed URL, webhook signature) "
            "exists",
            "Blade `{!! $x !!}` renders raw HTML — XSS sink",
            "Routes outside the `auth` middleware group, or routes with "
            "`->withoutMiddleware([...])`, need explicit per-action auth checks",
        ),
    ),
    "magento": (
        "Magento",
        ("php",),
        (
            "ACL via `etc/acl.xml`; webapi routes via `etc/webapi.xml` `<resources>` — flag "
            "routes set to `anonymous` doing sensitive work",
            "`$this->getRequest()->getParam('x')` is user input",
            "Plugin/observer code runs in core context — privilege escalation is easy if input "
            "isn't sanitized",
            "Customer data via `\\Magento\\Customer\\Api` requires customer ID; flag any read "
            "using user-supplied ID without ownership check",
        ),
    ),
    "mcp": (
        "MCP / agentic tools",
        ("typescript", "javascript", "python"),
        (
            "Tool inputs and retrieved content are untrusted data, not instructions.",
            "Tool schemas need allowlists, execution caps, and explicit filesystem/network "
            "boundaries.",
        ),
    ),
    "micronaut": (
        "Micronaut",
        ("java", "kotlin"),
        (
            "`@Secured(SecurityRule.IS_AUTHENTICATED)` on controller is the gate; `@PermitAll` "
            "opens it back up",
            "`@Body` / `@QueryValue` / `@PathVariable` are user input",
            "Reactive endpoints return `Mono`/`Flux` — auth check must be in the reactive "
            "chain, not just the handler signature",
            "Bean introspection (compile-time DI) means runtime config can't easily swap auth "
            "— flag config-driven gates",
        ),
    ),
    "nestjs": (
        "NestJS",
        ("typescript", "javascript"),
        (
            "`@UseGuards(...)` on controller or method is the auth check; missing guards on a "
            "`@Controller()` are a common gap",
            "`@Body()` / `@Query()` without a `class-validator` DTO is unvalidated input",
            "Global pipes/interceptors registered late or only in main.ts may not apply to "
            "e2e-test routes shipped to prod",
            "`@Public()` decorators that opt OUT of a global auth guard — confirm they are "
            "intentional",
        ),
    ),
    "nextjs": (
        "Next.js",
        ("typescript", "javascript"),
        (
            "Next.js `middleware.ts` runs at the edge and is NOT sufficient auth — too easy to "
            "misconfigure or bypass via routes that escape the matcher",
            "Server Actions are publicly callable POST endpoints — every one needs explicit auth "
            "+ authorization checks",
            "`JSON.stringify()` inside `dangerouslySetInnerHTML` or inline `<script>` tags is XSS "
            "unless the output escapes `</` (look for `safeJsonStringify` or `\\u003c`)",
            "`searchParams` and dynamic route segments (`[id]`, `[...slug]`) are user-controlled "
            "— treat them as untrusted in middleware too",
            "`unstable_cache` / `revalidateTag` on user-supplied keys can leak across tenants",
        ),
    ),
    "nuxt": (
        "Nuxt",
        ("typescript", "javascript"),
        (
            "`server/api/**/*.ts` files become public endpoints — `defineEventHandler(async (event) "
            "=> ...)` needs explicit auth",
            "`getQuery(event)` / `readBody(event)` / `getRouterParam(event)` are user input",
            "Server middleware in `server/middleware/` runs on EVERY request including `/_nuxt/*` — "
            "check for ordering",
            "`useRuntimeConfig().public.*` keys leak to the client bundle; only top-level "
            "`runtimeConfig.<key>` stays server-only",
        ),
    ),
    "phoenix": (
        "Phoenix (Elixir)",
        ("elixir",),
        (
            "Pipelines (`pipeline :api do plug :auth_token end`) are the gate; routes in a "
            "`scope` without the right pipeline are public",
            '`conn.params` / `conn.body_params` are user input; raw `Repo.query!("... #{x}")` is '
            "SQLi (`Ecto.Adapters.SQL.query!` parameterized form is the safe one)",
            "`Phoenix.HTML.raw(x)` skips escaping — XSS sink",
            'LiveView `handle_event("name", params, socket)` runs server-side — flag handlers '
            "that don't authorize the action against `socket.assigns.current_user`",
            "Routes can use `live_session` with `on_mount` for auth — verify the on_mount auth "
            "check actually runs",
        ),
    ),
    "poem": (
        "Poem",
        ("rust",),
        (
            "`#[handler]` functions have type-driven extractors (`Json`, `Query`, `Path`) — "
            "extractors validate structure, not content",
            "Endpoint composition via `.with(middleware)` and `.around(handler_fn)` — middleware "
            "must wrap the auth path",
            "`OpenApiService` exposes the schema — confirm prod doesn't ship Swagger UI on a "
            "sensitive route",
        ),
    ),
    "rails": (
        "Ruby on Rails",
        ("ruby",),
        (
            "`skip_before_action :authenticate_user!` on a controller (or specific action) — "
            "confirm it's intentional and not on a write endpoint",
            "Strong parameters: `params.require(:x).permit(...)` is the mass-assignment guard; "
            "`params[:x]` directly into `Model.update(...)` is the bug",
            "`raw(x)` / `x.html_safe` / `<%== %>` on user input is XSS — note that bare `<%= %>` "
            "auto-escapes in Rails ≥ 3 and is safe; flag the explicit unescape forms, not standard "
            "ERB output",
            "`find_by_sql` / `where(\"col = '#{x}'\")` is SQL injection — `where(col: x)` is the "
            "safe form",
            "`redirect_to params[:return_to]` is an open redirect; check for an allowlist",
        ),
    ),
    "react": (
        "React",
        ("typescript", "javascript"),
        (
            "`dangerouslySetInnerHTML` with any user-influenceable string is XSS — DB values and "
            "usernames count as user-controlled",
            "Refs and effects that touch `document.location` / `window.opener` can become "
            "open-redirect or tabnabbing sinks",
            "Server-rendered JSON in `<script>` tags must escape `</` to be XSS-safe",
        ),
    ),
    "remix": (
        "Remix",
        ("typescript", "javascript"),
        (
            "`action` functions are publicly callable POST endpoints — every one needs explicit "
            "auth (`requireUserId(request)` or equivalent)",
            "`loader` functions can return PII; avoid passing raw DB rows — shape the response",
            "Resource routes (no default export) accept any HTTP method by default — explicit "
            "method handlers are safer",
            "`redirect()` to user-controlled paths — confirm validation/allowlist before flagging",
        ),
    ),
    "rocket": (
        "Rocket",
        ("rust",),
        (
            "Request guards are the auth gate (`fn from_request(...) -> Outcome<...>`) — handlers "
            "without a guard are public",
            "`#[derive(FromForm)]` / `Json<T>` deserialize user input — types validate structure "
            "only",
            "`#[catch(404)]` and similar can leak internal info if the catcher renders raw error "
            "data",
            "Fairings (Rocket middleware) have lifecycle hooks; `on_request` running BEFORE auth "
            "guard is the wrong layer for security",
        ),
    ),
    "roda": (
        "Roda",
        ("ruby",),
        (
            "Roda uses a routing tree (`r.on`/`r.is`/`r.get`...) — auth must be at the tree node "
            "that wraps the handler, not just at the leaf",
            "`r.params` is user input; same sinks as Sinatra",
            "Plugins (`plugin :csrf`, `plugin :authentication`) are off by default — confirm "
            "they're loaded",
            "`r.run(other_app)` mounts subapps — they don't inherit the parent tree's auth "
            "automatically",
        ),
    ),
    "sanic": (
        "Sanic",
        ("python",),
        (
            "`@app.middleware('request')` runs on every request — flag auth checks that only run "
            "on specific blueprints",
            "`request.args` / `request.json` / `request.form` / `request.files` are user input",
            "`response.html()` does NOT auto-escape — use a templating layer or sanitize",
            "Worker count + concurrency means request-local state via globals is unsafe",
        ),
    ),
    "sinatra": (
        "Sinatra",
        ("ruby",),
        (
            "No built-in auth — every `get '/path' do ... end` is public unless a `before do ... "
            "end` hook enforces a check",
            '`params[:x]` is user input — concatenation into SQL via `Sequel.lit("...#{x}...")` '
            "or `where(\"col = '#{x}'\")` is SQLi",
            "`erb` templates auto-escape only when `escape_html: true` is set; the default is "
            "OFF — confirm the setting",
            "`send_file(params[:f])` without a path containment check is path traversal",
        ),
    ),
    "slim": (
        "Slim",
        ("php",),
        (
            "Slim has no built-in auth — middleware via `->add(...)` order matters; routes outside "
            "the `->group(...)` aren't covered",
            "`$request->getQueryParams()` / `getParsedBody()` / `getAttribute()` are user input",
            "`->add()` order is reversed at execution: last-added runs first; flag routes with auth "
            "middleware added BEFORE logging",
        ),
    ),
    "socketio": (
        "Socket.IO",
        ("typescript", "javascript"),
        (
            "`io.use(authMiddleware)` is the auth gate; absent or post-route registrations "
            "leave events unauthenticated",
            "`socket.on('event', handler)` payloads are user input — validate before persisting "
            "/ emitting",
            "`socket.handshake.auth` is client-supplied; rely on the validated session, not the "
            "handshake claim",
            "Broadcasting to a room without scoping to the authenticated user is cross-tenant "
            "data leakage",
        ),
    ),
    "solidstart": (
        "SolidStart",
        ("typescript", "javascript"),
        (
            "`'use server'` exports and `action()` / `cache()` factories are publicly "
            "callable — auth must live INSIDE the function",
            "Server functions run with the request context but lack a built-in CSRF token; "
            "verify the deploy uses SameSite cookies",
            "`createAsync(() => fetch...)` data fetched server-side can include PII — shape "
            "the return",
        ),
    ),
    "spring": (
        "Spring",
        ("java", "kotlin"),
        (
            "`SecurityFilterChain` / `HttpSecurity.authorizeHttpRequests` is the gate — "
            "`permitAll()` on a sensitive path is the bug",
            '`@PreAuthorize("...SpEL...")` evaluates against the authenticated principal; flag '
            "handlers without it",
            "`@RequestParam` / `@PathVariable` / `@RequestBody` are user input — Bean validation "
            "(`@Valid`) only checks structure",
            "`@ResponseBody` returning entity classes can over-expose DB columns; use a DTO",
            'CORS config (`@CrossOrigin("*")`) with credentials enabled is CSRF-via-fetch',
        ),
    ),
    "starlette": (
        "Starlette",
        ("python",),
        (
            "Auth lives in `AuthenticationMiddleware` + a backend; routes without it are public",
            "`Mount('/sub', app)` composes apps — the child app inherits NO middleware unless "
            "re-applied",
            "`request.query_params` / `request.json()` / `request.form()` are user input; same "
            "sinks as FastAPI",
            "WebSocketRoute handlers run on a long-lived connection — auth check should be on "
            "the OPEN handshake, not after",
        ),
    ),
    "sveltekit": (
        "SvelteKit",
        ("typescript", "javascript"),
        (
            "`+server.ts` exports (`GET`/`POST`/...) are public — auth must live IN the "
            "handler, not just the page",
            "Form `actions` are server-callable from any client — confirm auth and CSRF "
            "(SvelteKit has built-in CSRF but check overrides)",
            "`load` functions in `+page.server.ts` run server-side and can leak via the "
            "streamed `data` prop — shape the return",
            "`hooks.server.ts` is the right place for global auth; flag routes that bypass it",
        ),
    ),
    "symfony": (
        "Symfony",
        ("php",),
        (
            "Routes without `#[IsGranted]` or `security:` config are public — confirm "
            "controller/method has an auth gate",
            "`$request->get('x')` / `$request->query->get('x')` / `$request->request->get('x')` "
            "are user input",
            "Twig auto-escapes by default; `|raw` filter or `{% autoescape false %}` blocks are "
            "XSS sinks",
            "`#[Route(requirements: ['id' => '\\d+'])]` regex constraints are easy to fool — "
            "auth checks must use the resolved entity, not the param",
            "`security.yaml` `access_control` rules match top-down; a permissive earlier rule "
            "defeats a stricter later one",
        ),
    ),
    "terraform": (
        "Terraform / IaC",
        ("terraform",),
        (
            "Flag public ingress on sensitive ports, wildcard IAM action+resource, plaintext "
            "secrets, and unencrypted stores.",
            "Module/source refs should be pinned to immutable versions.",
        ),
    ),
    "tide": (
        "Tide",
        ("rust",),
        (
            "Middleware via `app.with(...)` runs in registration order — auth before logging is "
            "right",
            "`req.body_json::<T>()` / `req.query::<T>()` / `req.param('x')` are user input",
            "Tide's response macros don't HTML-escape — explicit escaping is the dev's "
            "responsibility",
        ),
    ),
    "tonic": (
        "Tonic (gRPC)",
        ("rust",),
        (
            "Per-method auth via `Interceptor` is the right gate; absent or method-skipping "
            "interceptors leave RPCs unauthenticated",
            "`Request<T>` body deserializes via prost — type-safe, but unbounded "
            "`repeated`/`bytes` fields can DoS the server",
            "Streaming RPCs (`tonic::Streaming<T>`) — auth check at stream open, not on each "
            "message",
            "`Status::unauthenticated()` / `permission_denied()` are the canonical denials — "
            "confirm code uses them, not generic `internal`",
        ),
    ),
    "tornado": (
        "Tornado",
        ("python",),
        (
            "`@tornado.web.authenticated` is the auth gate — handlers without it are public; "
            "`get_current_user()` must be implemented per app",
            "`self.get_argument('x')` / `self.get_body_argument(...)` are user input",
            "Tornado templates auto-escape by default; `{% raw x %}` is the explicit unescape sink",
            "`tornado.escape.xhtml_escape` is the safe form for HTML; absence on user-influenced "
            "content is XSS",
        ),
    ),
    "vapor": (
        "Swift Vapor",
        (),
        (
            "Auth via middleware: `app.grouped(User.guardMiddleware()).get(...)` — routes outside "
            "the grouped scope are public",
            '`req.parameters.get("x")` / `req.query` / `req.content.decode(...)` are user input',
            'Fluent (Vapor\'s ORM) — raw SQL via `req.db.raw("... \\(x)")` is injection; '
            "`.where(\\.$name == x)` is parameterized",
            "Sessions (`req.session.data`) and JWTs (`req.jwt.verify(...)`) — verify signature "
            "algorithm pinning",
        ),
    ),
    "warp": (
        "Warp",
        ("rust",),
        (
            "Filters compose via `.and(...)` — auth filter must precede the body extractor in the "
            "chain",
            "`warp::path!()` macro — patterns end-anchor by default, but `warp::any()` matches "
            "everything (review uses)",
            "`warp::body::content_length_limit(N)` is essential; absent uses can DoS the server",
            "Error rejections via `Rejection` — confirm error responses don't leak internal "
            "types/messages",
        ),
    ),
    "wordpress": (
        "WordPress",
        ("php",),
        (
            "`wp_ajax_nopriv_*` actions are unauthenticated by design — sensitive operations "
            "belong on `wp_ajax_*` only",
            "`'permission_callback' => '__return_true'` on `register_rest_route` is a public "
            "route — confirm intent",
            '`$wpdb->query("... $user_input ...")` is SQL injection; `$wpdb->prepare()` with '
            "placeholders is the safe form",
            "`wp_redirect($_GET['redirect'])` without `wp_validate_redirect()` is open-redirect",
            "Capability checks (`current_user_can()`) gate admin actions — flag handlers that "
            "skip them",
        ),
    ),
    "workers": (
        "Cloudflare Workers / Edge",
        ("typescript", "javascript"),
        (
            "`export default { fetch(req, env, ctx) }` is the only entry — auth lives in "
            "`fetch`, no framework gates",
            "`env.<BINDING>` exposes KV / R2 / D1 / secrets — review whether bindings are "
            "over-permissioned",
            "Workers can't `require('fs')` / `child_process`; common Node patterns are absent "
            "(verify imports compile in Workers runtime)",
            "`caches.default` keys include the full URL — query strings poison the cache unless "
            "normalized",
        ),
    ),
    "yii": (
        "Yii",
        ("php",),
        (
            "Every `actionXxx()` on a Controller is publicly accessible by default — `behaviors()` "
            "is the place to wire AccessControl",
            "`Yii::$app->request->post('x')` / `get('x')` are user input; mass assignment via "
            "`$model->load($data)` is the bug if `safeAttributes()` isn't restricted",
            "`Html::encode()` is the safe form; `Html::decode()` and `echo $userInput` are XSS sinks",
            "AR query: `findOne(['id' => $id])` is parameterized; raw "
            '`Yii::$app->db->createCommand("...$id...")` is SQL injection',
        ),
    ),
}

SLUG_NOTES: dict[str, str] = {
    "agentic-untrusted-prompt-input": "Separate trusted instructions from untrusted retrieved/user "
    "content.",
    "all-route-handlers": "Coarse entry-point flag — confirm the handler reaches user input AND lacks "
    "auth/validation before flagging.",
    "all-route-handlers-other": "Generic HTTP entry; trace input → sink before classifying.",
    "all-server-actions": "Server Actions are public POST endpoints; flag any that don't explicitly "
    "check auth + ownership.",
    "android-manifest-export": "Exported component — confirm `android:permission=` guards the IPC "
    "surface; pre-API-31 implicit intent-filters are exported by default.",
    "apex-rest-resource": "`without sharing` BYPASSES row-level security — confirm intentional and "
    "that the resource can't be invoked by unprivileged users.",
    "auth-bypass": "Look for inverted booleans, early returns that skip checks, and `if "
    "(process.env.X) skipAuth()` patterns.",
    "azure-function-handler": "Azure Function — `AuthorizationLevel.Anonymous` is public; queue/blob "
    "triggers still receive user-influenced payloads.",
    "cache-key-poisoning": "Cache keys derived from request headers/cookies (User-Agent, Cookie, "
    "X-Forwarded-*) without normalization are the bug.",
    "clj-ring-handler": "Weak entry-point candidate — confirm a `wrap-*` middleware (auth, "
    "anti-forgery) is in the chain before this handler.",
    "cors-wildcard": "`origin: true` + `credentials: true` is the high-severity shape; static `*` "
    "without credentials is usually fine.",
    "cr-kemal-route": "Weak entry-point candidate — Kemal has no built-in auth; confirm a `before_*` "
    "filter intercepts.",
    "cross-tenant-id": "User-supplied teamId/userId in DB queries — confirm the authenticated "
    "identity is used for the ownership check, not the request param alone.",
    "dangerous-html": "DB-stored HTML is still untrusted — flag unless there's a sanitizer "
    "(DOMPurify, sanitize-html) BETWEEN the data and the render.",
    "dart-shelf-handler": "Weak entry-point candidate — Shelf has no built-in auth; confirm "
    "`Pipeline().addMiddleware(...)` registration order covers this route.",
    "debug-endpoint": "Routes guarded by `process.env.NODE_ENV === 'development'` can ship to prod "
    "via env misconfig — flag if the route does anything sensitive.",
    "dev-auth-bypass": "`if (env === 'dev') return adminUser` patterns — verify the env check can't "
    "be tricked, and that the path isn't reachable in prod.",
    "dotnet-aspnet-controller": "Weak entry-point candidate — confirm `[Authorize]` covers the "
    "action; `[AllowAnonymous]` opens it back up.",
    "dotnet-azure-function": "Weak entry-point candidate — `AuthorizationLevel.Anonymous` is public; "
    "function/admin keys ≠ user identity.",
    "dotnet-minimal-api": "Weak entry-point candidate — confirm `.RequireAuthorization()` is chained "
    "on this map; `app.MapGet(...)` alone is public.",
    "dotnet-razor-pages": "Weak entry-point candidate — confirm `[Authorize]` on the page model and "
    "`[ValidateAntiForgeryToken]` on POST handlers.",
    "dotnet-sql-raw": 'ADO.NET/Dapper/EF Core raw SQL — concat or `$"..."` interpolation into '
    "`SqlCommand`/`Query`/`Execute`/`FromSqlRaw` is injection. EF Core "
    '`FromSqlInterpolated($"... {x} ...")` parameterizes correctly; only '
    '`FromSqlRaw` with concat or `$"..."` is the bug.',
    "env-exposure": "Secrets reaching client bundles via `NEXT_PUBLIC_` / `VITE_` / build-time "
    "inlining — flag only if the env var holds a credential.",
    "erl-cowboy-handler": "Weak entry-point candidate — auth check must happen in `init/2` before any "
    "state-changing call.",
    "ex-phoenix-controller": "Weak entry-point candidate — confirm this route's `scope` uses an "
    "auth-bearing pipeline (e.g. `pipe_through [:api, :authenticated]`).",
    "expensive-api-abuse": "LLM/AI/paid-API endpoints without per-user rate limits or auth — confirm "
    "the cost-per-call is non-trivial before flagging.",
    "framework-server-action": "Verify the action calls auth() / requireUser() before any DB write or "
    "external call.",
    "gcp-cloud-function": "Cloud Function — confirm not deployed with `--allow-unauthenticated`; "
    "integrate Identity Platform / Firebase Auth for user identity.",
    "github-workflow-security": "PR-triggered workflows with secrets, mutable actions, or shell "
    "interpolation are supply-chain risks.",
    "go-buffalo-route": "Weak entry-point candidate — confirm `app.Use(auth)` is registered before "
    "this route or resource.",
    "go-chi-route": "Weak entry-point candidate — confirm `r.Use(auth)` is in scope and no "
    "`r.Mount(...)` later short-circuits the inheritance.",
    "go-cobra-command": "Privileged CLI — flag interpolated user args reaching shell/SQL and any "
    "logging of `cmd.Flags()`.",
    "go-command-injection": '`exec.Command("sh", "-c", interpolated)` is the bug; '
    '`exec.Command("cmd", arg1, arg2)` with discrete args is generally safe.',
    "go-echo-route": "Weak entry-point candidate — confirm `e.Use(...)` auth precedes the route, and "
    'the route isn\'t on the bare engine when only `g := e.Group("/api", auth)` is '
    "guarded.",
    "go-fiber-route": "Weak entry-point candidate — confirm `app.Use(auth)` precedes the route and "
    "that no route-level middleware overrides the group middleware silently.",
    "go-gin-route": "Weak entry-point candidate — confirm no auth `r.Use(...)` precedes the route "
    "registration in this group.",
    "go-gorilla-route": "Weak entry-point candidate — confirm `router.Use(auth)` covers this "
    "subrouter; `PathPrefix(...).Handler(other)` doesn't inherit.",
    "go-sql-raw": "database/sql, GORM, sqlx, pgx — `fmt.Sprintf` or `+` concat into "
    '`Query`/`Exec`/`Raw` is SQL injection. `db.Query("... $1 ...", val)` '
    '(pgx/database/sql) and `db.Where("col = ?", val)` (GORM) are the safe shapes.',
    "go-ssrf": "Concatenated URL passed to `http.Get` / `client.Do` without an allowlist — internal "
    "hosts are the high-severity case.",
    "iam-permissions": "Wildcards in Action AND Resource together are the dangerous shape; one or the "
    "other can be intentional.",
    "ios-url-scheme": "URL handler entry — `application(_:open:)` and `scene(_:openURLContexts:)` "
    "receive attacker-controlled URLs; review universal-link host association.",
    "js-astro-endpoint": "Weak entry-point candidate — `pages/api/*` exports are public; SSR pages "
    "with `prerender = false` need handler-level auth.",
    "js-bullmq-processor": "`job.data` is producer-supplied — re-validate trust boundary at the queue "
    "if any web handler can enqueue.",
    "js-bun-serve": "Raw HTTP entry — no framework gates, all auth/validation lives in the `fetch` "
    "handler.",
    "js-deno-route": "Weak entry-point candidate — Deno has no built-in auth; middleware order is "
    "hand-rolled.",
    "js-express-route": "Weak entry-point candidate — confirm the handler reads `req.*` data AND "
    "lacks an auth wrapper / validator before flagging.",
    "js-fastify-route": "Weak entry-point candidate — confirm no `preHandler`/`onRequest` auth hook "
    "AND no schema validation before flagging.",
    "js-graphql-resolver": "Per-resolver auth needed — confirm `context.user` (or schema directive) "
    "gates the field; no global auth applies.",
    "js-hapi-route": "Weak entry-point candidate — confirm `auth: false` isn't set and that a default "
    "strategy was registered.",
    "js-hono-route": "Weak entry-point candidate — confirm no auth `app.use(...)` precedes the route "
    "registration before flagging.",
    "js-koa-route": "Weak entry-point candidate — confirm auth middleware is registered BEFORE the "
    "route via `app.use(auth)`.",
    "js-nestjs-controller": "Weak entry-point candidate — confirm no `@UseGuards` and no "
    "class-validator DTO before flagging.",
    "js-nosql-injection": "MongoDB / Mongoose — `$where` with concat/function/template lets attackers "
    "run arbitrary JS in the DB. `find(JSON.parse(req.body...))` accepts "
    "attacker operator keys (`$ne`, `$gt`) — coerce to typed query first. `new "
    "RegExp(req.*)` is ReDoS-shaped.",
    "js-nuxt-route": "Weak entry-point candidate — `defineEventHandler` files in `server/api` are "
    "public; confirm auth runs in handler or `server/middleware`.",
    "js-remix-route": "Weak entry-point candidate — `loader`/`action` exports are public; verify "
    "`requireUserId(request)` (or equivalent) runs first.",
    "js-socketio-handler": "Weak entry-point candidate — confirm `io.use(auth)` runs before any "
    "`socket.on('event', ...)` handler executes.",
    "js-solidstart-action": "Weak entry-point candidate — `'use server'` exports are publicly "
    "callable; auth must live IN the function.",
    "js-sql-raw": "Raw-SQL across pg/mysql2/TypeORM/Sequelize/Knex/Kysely/postgres.js — flag string "
    "concat or template interpolation into SELECT/INSERT/UPDATE/DELETE; parameterized "
    "forms (`$1`, `:name`, prepared statements with separate args) are the safe shape. "
    "`sql\\`...\\`` tagged templates from libraries that escape (drizzle, postgres.js "
    "without `.unsafe`) are safe.",
    "js-sveltekit-route": "Weak entry-point candidate — `+server.ts` and form `actions` are public; "
    "confirm `hooks.server.ts` enforces auth and isn't bypassed.",
    "js-workers-fetch": "Worker default-export `fetch` is the only entry — auth lives entirely in the "
    "handler; review `env.<BINDING>` permissions.",
    "jvm-jaxrs-resource": "Weak entry-point candidate — confirm `@RolesAllowed(...)` is set; absence "
    "on a `@Path` resource is public.",
    "jvm-ktor-route": "Weak entry-point candidate — confirm this route is inside an "
    "`authenticate(...) { ... }` block.",
    "jvm-micronaut-controller": "Weak entry-point candidate — confirm `@Secured(...)` covers this "
    "method; `@PermitAll` opens it.",
    "jvm-spring-controller": "Weak entry-point candidate — confirm `SecurityFilterChain` / "
    "`@PreAuthorize` covers this method; `permitAll()` is the bug.",
    "jvm-sql-raw": "JDBC/JPA/Hibernate/JdbcTemplate/MyBatis/jOOQ/Exposed raw SQL — string concat into "
    "`executeQuery`/`createQuery`/`createNativeQuery` is injection even on "
    "PreparedStatement (concat happens before binding). MyBatis `${param}` is unsafe; "
    "`#{param}` parameterizes.",
    "jwt-handling": "Look for `algorithm: 'none'`, missing `algorithms: ['HS256']` pinning, or "
    "skipping `verify()` in dev branches.",
    "lambda-aws-handler": "Lambda handler — confirm `event.requestContext.authorizer` is read; "
    "over-permissioned IAM role widens RCE blast radius.",
    "lua-ngx-exec": "`ngx.exec` / `ngx.redirect` / `os.execute` with concatenated request data is "
    "RCE-shaped on Lua/OpenResty.",
    "lua-shared-dict-poisoning": "Writes to `ngx.shared` from request data persist across requests — "
    "flag if the read path trusts the cached value.",
    "mcp-tool-handler": "Review tool schema, capability allowlist, and side-effect boundaries.",
    "missing-auth": "Weak candidate — only flag if no auth wrapper, no role check, AND "
    "user-controlled input reaches a sink.",
    "nextjs-middleware-only-auth": "Next.js middleware.ts alone is NOT sufficient — confirm a backend "
    "framework guard wraps the handler too.",
    "non-atomic-operation": "Read-then-write patterns without a lock / transaction / atomic op are "
    "TOCTOU; flag only if the resource is shared across requests.",
    "object-injection": "User-controlled keys into `obj[x] = v` without an allowlist enable "
    "prototype-pollution / overwriting safe defaults.",
    "open-redirect": "Flag only if there's no allowlist, origin check, or hash-only redirect; "
    "relative paths starting with `//` are still external.",
    "path-traversal": "Flag if `path.join(root, userInput)` lacks a "
    "`path.resolve(...).startsWith(root)` containment check.",
    "php-cakephp-controller": "Weak entry-point candidate — `$this->Auth->allow(...)` lists public "
    "actions; confirm scope.",
    "php-codeigniter-controller": "Weak entry-point candidate — confirm a Filter in "
    "`app/Config/Filters.php` covers this route.",
    "php-drupal-controller": "Weak entry-point candidate — `*.routing.yml` `_permission` of `access "
    "content` is permissive; confirm scope.",
    "php-laravel-route": "Weak entry-point candidate — confirm the route is outside the `auth` "
    "middleware group AND user input reaches a sink before flagging.",
    "php-magento-controller": "Weak entry-point candidate — confirm webapi.xml resource is not "
    "`anonymous` for sensitive actions.",
    "php-slim-route": "Weak entry-point candidate — middleware via `->add(...)` is reverse-order; "
    "confirm auth attaches to the right group.",
    "php-sql-raw": "PDO/mysqli/Doctrine raw SQL — concat (`.`) into `query`/`exec`/`executeQuery` is "
    'SQL injection. PDO `prepare("... ? ...")` + `execute([val])` and Doctrine '
    "`executeQuery(\"... :x ...\", ['x' => val])` are the safe forms.",
    "php-symfony-controller": "Weak entry-point candidate — confirm `#[IsGranted]` / `security.yaml` "
    "access_control covers this route.",
    "php-wordpress-rest": "Weak entry-point candidate — flag `permission_callback => __return_true` "
    "and `wp_ajax_nopriv_*` on sensitive operations.",
    "php-yii-controller": "Weak entry-point candidate — every public `actionXxx()` is reachable; "
    "confirm `behaviors()` wires AccessControl.",
    "public-endpoint": "Confirm the endpoint truly has no auth (not just a permissive guard) and that "
    "it returns sensitive data.",
    "py-aiohttp-route": "Weak entry-point candidate — confirm an `@web.middleware` auth check is "
    "registered before the route.",
    "py-airflow-dag": "Privileged scheduler surface — flag interpolated template fields (`{{ params.x "
    "}}`) reaching Bash/SQL/HTTP operators.",
    "py-bottle-route": "Weak entry-point candidate — Bottle has no built-in auth; flag handlers with "
    "no decorator chain enforcing it.",
    "py-celery-task": "Background-job surface — confirm queue producer authenticates the user; pickle "
    "serializer = unsafe deserialization.",
    "py-django-view": "Weak entry-point candidate — confirm no `LoginRequiredMixin` / "
    "`@login_required` / DRF `permission_classes` AND that user input reaches a "
    "sink before flagging.",
    "py-falcon-resource": "Weak entry-point candidate — confirm a middleware/hook sets "
    "`req.context.user` BEFORE the on_<method> runs.",
    "py-fastapi-route": "Weak entry-point candidate — confirm no `Depends(auth)` / `Security(...)` "
    "and that input reaches a sink before flagging.",
    "py-flask-route": "Weak entry-point candidate — confirm no `@login_required` / `before_request` "
    "auth hook before flagging.",
    "py-nosql-injection": 'PyMongo — `$where: f"..."` runs JS in MongoDB. `coll.find({"$regex": '
    "request.args.get(...)})` is ReDoS / injection. Use typed query keys, "
    "validate operators server-side.",
    "py-sanic-route": "Weak entry-point candidate — confirm `@app.middleware('request')` auth check "
    "applies to this blueprint.",
    "py-sql-raw": "Raw-SQL across SQLAlchemy/psycopg/pymysql/sqlite3/asyncpg/Django ORM — f-string, "
    "`%` formatting, `.format()`, and `+` concat into SQL are injection. The safe shape "
    'is `cursor.execute("... %s ...", (val,))` (psycopg) or `text("... :x '
    '...").bindparams(x=val)` (SQLAlchemy).',
    "py-starlette-route": "Weak entry-point candidate — confirm AuthenticationMiddleware is wired and "
    "applies to this Mount.",
    "py-tornado-handler": "Weak entry-point candidate — confirm `@tornado.web.authenticated` is on "
    "each method handler.",
    "rate-limit-bypass": "Sensitive operations (auth, password reset, expensive APIs) without "
    "rate-limit middleware are the high-signal cases.",
    "rb-grape-endpoint": "Weak entry-point candidate — confirm a `before do ... end` or `helpers do "
    "... end` block enforces auth.",
    "rb-hanami-action": "Weak entry-point candidate — confirm a `before` callback or middleware "
    "enforces auth on this Action class.",
    "rb-rails-controller": "Weak entry-point candidate — confirm `skip_before_action "
    ":authenticate_user!` is intentional or that no auth callback is in scope.",
    "rb-roda-route": "Weak entry-point candidate — auth must wrap the tree node, not just the leaf; "
    "confirm scope.",
    "rb-sinatra-route": "Weak entry-point candidate — confirm a `before do ... end` filter enforces "
    "auth on this route.",
    "rb-sql-raw": "ActiveRecord/Sequel/pg-gem raw SQL — `#{}` interpolation in "
    '`find_by_sql`/`where("...")`/`Sequel.lit` is injection. `where(col: val)` (AR) and '
    "`where(Sequel[:col] => val)` (Sequel) are parameterized.",
    "rce": "Distinguish dynamic command (string concat → exec) from static command with sanitized "
    "args (which is fine).",
    "rs-actix-route": "Weak entry-point candidate — confirm `App::new().wrap(auth)` covers this scope "
    "and extractors validate content (not just structure).",
    "rs-axum-route": "Weak entry-point candidate — confirm `.layer(auth_layer)` precedes the route; "
    "check `.merge` / `.nest` order.",
    "rs-lambda-runtime": "Lambda handler — confirm `event.payload.request_context.authorizer` claims "
    "are read; cold-start global state can leak across tenants.",
    "rs-poem-route": "Weak entry-point candidate — confirm `.with(auth)` or `.around(auth)` wraps "
    "this endpoint.",
    "rs-rocket-route": "Weak entry-point candidate — confirm a request guard (`fn from_request`) runs "
    "before this handler.",
    "rs-sql-raw": "Rust runtime SQL via `sqlx::query(&format!(...))` / "
    "`diesel::sql_query(format!(...))` / `Statement::from_string(format!(...))` is "
    'injection. Note: the COMPILE-TIME-checked `sqlx::query!("... {}", arg)` macro form '
    "is parameterized and SAFE — don't flag those.",
    "rs-tide-route": "Weak entry-point candidate — confirm `app.with(auth_middleware)` was registered "
    "before this route.",
    "rs-tonic-grpc": "Per-method gRPC auth — confirm an Interceptor checks every method, not just "
    "selected ones.",
    "rs-warp-filter": "Weak entry-point candidate — auth filter must precede the body extractor in "
    "the `.and()` chain.",
    "secret-env-var": "Direct env var reads in client-bundled code (NEXT_PUBLIC_*) are the bug — "
    "confirm the file isn't server-only.",
    "secret-in-fallback": '`process.env.X || "hardcoded"` is the bug — only flag when the fallback '
    'looks like a real credential, not `"localhost"`.',
    "secret-in-log": "Logging full headers, request bodies, or error objects can leak Authorization "
    "tokens; flag if the log destination is durable.",
    "secrets-exposure": "Distinguish real secrets from example values, dummy tokens in tests, and "
    "rotated/expired markers.",
    "service-entry-point": "Coarse flag — verify there's an actual auth gap, not just an "
    "internal-only handler reachable via service mesh.",
    "spread-operator-injection": "Object spread precedence: later keys win. `{role: 'user', "
    "...userInput}` is the bug — a trailing spread of "
    "attacker-controlled `userInput` can overwrite the earlier `role`. "
    "`{...userInput, role: 'user'}` is the safe order. Flag the "
    "trailing-spread shape.",
    "sql-injection": "Flag string-concat / template-literal SQL only if the variable is "
    "user-reachable; ORM `where({col: x})` is safe.",
    "ssrf": "Check whether the URL host is constrained to an allowlist, blocked from RFC1918, or "
    "proxied via a vetted URL parser.",
    "swift-vapor-route": "Weak entry-point candidate — confirm this route is inside a "
    "`.grouped(guardMiddleware())` scope.",
    "test-header-bypass": "`x-test-*` / `x-bypass-*` headers honored in handler code are the classic "
    "prod-leakage bug.",
    "unsafe-redirect": "Verify the redirect path passes through a validation function and that the "
    "validator can't be bypassed via encoding.",
    "use-server-export": "Every `'use server'` export is publicly callable — auth must live IN the "
    "function, not just on the calling page.",
    "webhook-handler": "Confirm signature verification (Stripe, GitHub, Shopify, Slack) happens "
    "BEFORE the body is parsed/processed.",
    "xss": "Check escape state at every step; raw concat into HTML, JSON-in-script without "
    "`</`-escape, and ref.innerHTML are the usual sinks.",
}
