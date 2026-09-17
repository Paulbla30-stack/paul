"""
OpenClaw Cloud Support

Everything needed to run the agent as the first-class process on a cloud
instance (currently AWS EC2):

- ``imds``      – IMDSv2 client for instance identity, user data and tags
- ``bootstrap`` – turns user data / metadata into agent configuration
- ``headless``  – non-interactive agent loop with a local status endpoint
"""

from openclaw.cloud.imds import IMDSClient  # noqa: F401
from openclaw.cloud.headless import HeadlessRunner  # noqa: F401
