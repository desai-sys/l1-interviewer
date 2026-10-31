#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stack import L1InterviewerStack

app = cdk.App()
L1InterviewerStack(
    app,
    "L1InterviewerStack",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=os.environ.get("CDK_DEFAULT_REGION")),
)
app.synth()
