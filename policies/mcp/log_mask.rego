# Decision logs keep the decision and the request's shape, never its content
# (CLAUDE.md, rule 6): tool arguments, raw bodies and credentials are masked.
package system.log

import rego.v1

mask contains "/input/attributes/request/http/body"
mask contains "/input/attributes/request/http/raw_body"
mask contains "/input/attributes/request/http/headers/authorization"
mask contains "/input/attributes/request/http/headers/cookie"
mask contains "/input/parsed_body/params/arguments"
mask contains "/input/parsed_body/result"
mask contains "/input/parsed_body/error"
