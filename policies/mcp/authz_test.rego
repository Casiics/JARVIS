package mcp.authz_test

import rego.v1

import data.mcp.authz

domain_data := {"domain": "content", "endpoints": ["reel-tools"], "tools": ["store_reel"]}

request(method, path, body) := {
	"attributes": {"request": {"http": {
		"method": method,
		"path": path,
		"headers": {"content-type": "application/json"},
	}}},
	"parsed_path": [p | some p in split(trim_prefix(path, "/"), "/")],
	"parsed_query": {},
	"parsed_body": body,
}

call(name) := {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": {}}}

decide(req) := r if {
	r := authz.result with input as req with data.domain_tools as domain_data
}

test_listed_tool_is_allowed if {
	decide(request("POST", "/mcp/reel-tools", call("store_reel"))).allowed
}

test_allowed_request_is_tagged_with_the_domain if {
	decide(request("POST", "/mcp/reel-tools", call("store_reel"))).headers["x-jarvis-domain"] == "content"
}

test_unlisted_tool_is_denied if {
	r := decide(request("POST", "/mcp/reel-tools", call("send_email")))
	not r.allowed
	r.http_status == 403
}

test_lifecycle_and_listing_are_allowed if {
	every m in ["initialize", "notifications/initialized", "ping", "tools/list"] {
		decide(request("POST", "/mcp/reel-tools", {"jsonrpc": "2.0", "id": 1, "method": m})).allowed
	}
}

test_other_mcp_methods_are_denied if {
	every m in ["resources/read", "prompts/get", "sampling/createMessage", "logging/setLevel"] {
		not decide(request("POST", "/mcp/reel-tools", {"jsonrpc": "2.0", "id": 1, "method": m})).allowed
	}
}

test_unlisted_endpoint_is_denied if {
	not decide(request("POST", "/mcp/other", call("store_reel"))).allowed
	not decide(request("POST", "/mcp-test/reel-tools", call("store_reel"))).allowed
	not decide(request("POST", "/rest/workflows", call("store_reel"))).allowed
	not decide(request("POST", "/mcp/reel-tools/extra", call("store_reel"))).allowed
}

test_query_string_is_denied if {
	req := object.union(request("POST", "/mcp/reel-tools", call("store_reel")), {"parsed_query": {"a": ["1"]}})
	not decide(req).allowed
}

test_batch_and_non_jsonrpc_bodies_are_denied if {
	not decide(request("POST", "/mcp/reel-tools", [call("store_reel")])).allowed
	not decide(request("POST", "/mcp/reel-tools", {"id": 1, "method": "tools/call", "params": {"name": "store_reel"}})).allowed
}

test_non_json_content_type_is_denied if {
	req := request("POST", "/mcp/reel-tools", call("store_reel"))
	r := decide(json.patch(req, [{"op": "replace", "path": "/attributes/request/http/headers/content-type", "value": "text/plain"}]))
	not r.allowed
}

test_tool_name_must_be_a_string if {
	body := {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": ["store_reel"]}}
	not decide(request("POST", "/mcp/reel-tools", body)).allowed
}

test_arguments_must_be_an_object if {
	body := {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "store_reel", "arguments": "x"}}
	not decide(request("POST", "/mcp/reel-tools", body)).allowed
}

test_get_stream_is_refused_with_405 if {
	r := decide(request("GET", "/mcp/reel-tools", null))
	not r.allowed
	r.http_status == 405
}

test_get_on_unlisted_endpoint_is_403 if {
	decide(request("GET", "/mcp/other", null)).http_status == 403
}

test_session_delete_is_allowed if {
	decide(request("DELETE", "/mcp/reel-tools", null)).allowed
}

test_other_domains_tools_are_denied if {
	r := authz.result with input as request("POST", "/mcp/reel-tools", call("store_reel"))
		with data.domain_tools as {"domain": "trading", "endpoints": ["reel-tools"], "tools": ["place_paper_order"]}
	not r.allowed
}

test_empty_domain_data_denies_everything if {
	r := authz.result with input as request("POST", "/mcp/reel-tools", call("store_reel"))
		with data.domain_tools as {"domain": "private", "endpoints": [], "tools": []}
	not r.allowed
}
