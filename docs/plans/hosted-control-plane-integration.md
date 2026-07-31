# Hosted control-plane integration

Migration 029 provides the Core-side read broker primitive. The intended future
request path is:

```text
browser session -> Portal BFF -> control-plane delegation broker -> one Core read
```

The browser must never receive an `engd_`, `engsvc_`, or ordinary agent
credential. Portal remains an architectural reference and is unchanged by the
Core delegation slice.

The control plane should operate two service clients: a provisioning client
that owns bindings and a distinct broker with only `delegation.issue`. An
owner-created grant connects those identities without giving the broker
provisioning authority.

Future Portal work must proxy exactly one Core request, discard the delegated
credential, and use the response-loss recovery procedure documented in
`docs/ops/service-delegation.md`. Browser token delivery, cookies, redirects,
refresh, exchange chaining, and offline introspection remain prohibited.

Ordinary Core reads made through single-use delegation now satisfy the hosted
response contract: all delegated Bearer attempts receive `no-store`,
`no-cache`, `no-referrer`, and a correlated request ID on success or failure.
Boundary classification neither authenticates nor consumes the credential, and
the effective response request ID is also used for Core delegation audit
evidence. This is a response boundary for direct Core routes, not a generic
hosted proxy or a browser token-delivery mechanism.

Core now provides a separate purpose-bound review step-up primitive. It uses
the `engdr_` credential domain and the `delegation.review.issue` service
permission. It does not broaden the read broker.

Portal integration remains deferred. A future Portal BFF can request one queue
inspection or one proposed-item activation or rejection. It must not deliver
Core or service credentials to the browser.
