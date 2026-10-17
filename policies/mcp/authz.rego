# Policy gateway for MCP servers (docs/architecture.md, sections 3 and 7).
#
# Envoy sends every request to OPA (ext_authz) before it reaches a domain's MCP server, which
# is an n8n MCP Server Trigger. One OPA instance runs per domain and loads only that domain's
# data file (policies/mcp-tools/<domain>.json), so a domain can never call another's tools.
#
# Deny by default. The caller must present a workload certificate (mTLS, checked by Envoy
# against the workload CA) whose SPIFFE ID belongs to this OPA's domain:
# spiffe://jarvis.home/<domain>/<service>. Allowed are then JSON-RPC requests over POST to a
# listed endpoint for the lifecycle methods and tools/list, and tools/call for listed tool
# names only. Server-initiated streams (GET) get 405, which MCP clients handle by not opening
# one.
package mcp.authz

import rego.v1

lifecycle_methods := {
	"initialize",
	"notifications/initialized",
	"notifications/cancelled",
	"ping",
	"tools/list",
}

http := input.attributes.request.http

# Envoy fills source.principal from the URI SAN of the verified client certificate.
caller := object.get(input, ["attributes", "source", "principal"], "")

caller_prefix := sprintf("spiffe://jarvis.home/%s/", [data.domain_tools.domain])

caller_ok if {
	startswith(caller, caller_prefix)
	regex.match(`^[a-z][a-z0-9-]{0,39}$`, trim_prefix(caller, caller_prefix))
}

default result := {
	"allowed": false,
	"http_status": 403,
	"headers": {"x-jarvis-policy": "denied"},
	"body": "{\"error\":\"denied by policy\"}",
}

result := {
	"allowed": false,
	"http_status": 405,
	"headers": {"allow": "POST, DELETE", "x-jarvis-policy": "denied"},
	"body": "",
} if {
	http.method == "GET"
	caller_ok
	endpoint_ok
}

result := {
	"allowed": true,
	"headers": {"x-jarvis-domain": data.domain_tools.domain, "x-jarvis-caller": caller},
} if {
	caller_ok
	allow
}

endpoint_ok if {
	count(input.parsed_path) == 2
	input.parsed_path[0] == "mcp"
	input.parsed_path[1] in data.domain_tools.endpoints
	object.get(input, "parsed_query", {}) == {}
}

json_request if {
	http.method == "POST"
	startswith(lower(http.headers["content-type"]), "application/json")
	is_object(input.parsed_body)
	input.parsed_body.jsonrpc == "2.0"
}

# Ending a session carries no payload.
allow if {
	endpoint_ok
	http.method == "DELETE"
}

allow if {
	endpoint_ok
	json_request
	input.parsed_body.method in lifecycle_methods
}

allow if {
	endpoint_ok
	json_request
	input.parsed_body.method == "tools/call"
	is_string(input.parsed_body.params.name)
	input.parsed_body.params.name in data.domain_tools.tools
	arguments_ok
}

arguments_ok if not "arguments" in object.keys(input.parsed_body.params)

arguments_ok if is_object(input.parsed_body.params.arguments)
