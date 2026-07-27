"""Strategy domain knowledge shared across features.

Home for the *index-core strategy* reference (Story 2.5, FR6/FR10): what counts
as a user's stable, broad index-fund "core" vs. the rest. It lives here — not in
``coach`` (Epic 4's pipeline) nor in broker-specific code — because both the
portfolio view (Epic 2) and the Coach Engine (Epic 4, which recommends investing
"into your index core") consume the same definition.
"""
