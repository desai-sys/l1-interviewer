"""Build the app's container image inside AWS (CodeBuild) during `cdk deploy`.

No Docker is needed on the deploying machine: CDK uploads the app folder as a zip,
a custom resource starts a CodeBuild job that runs `docker build` + `docker push`
into ECR, and waits for it to finish before the ECS service is created/updated.
The image tag is the hash of the source folder, so a new image is built only
when the code changes.
"""
from aws_cdk import (
    CustomResource,
    Duration,
    RemovalPolicy,
    aws_codebuild as codebuild,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3_assets as s3_assets,
    custom_resources as cr,
)
from constructs import Construct

_HANDLER = """
import boto3

cb = boto3.client("codebuild")


def on_event(event, context):
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": event["PhysicalResourceId"]}
    props = event["ResourceProperties"]
    build = cb.start_build(
        projectName=props["ProjectName"],
        environmentVariablesOverride=[{"name": "IMAGE_TAG", "value": props["ImageTag"], "type": "PLAINTEXT"}],
    )["build"]
    return {"PhysicalResourceId": props["ImageTag"], "Data": {"BuildId": build["id"]}}


def is_complete(event, context):
    if event["RequestType"] == "Delete":
        return {"IsComplete": True}
    build = cb.batch_get_builds(ids=[event["Data"]["BuildId"]])["builds"][0]
    status = build["buildStatus"]
    if status == "SUCCEEDED":
        return {"IsComplete": True}
    if status == "IN_PROGRESS":
        return {"IsComplete": False}
    link = build.get("logs", {}).get("deepLink", "see the CodeBuild console")
    raise RuntimeError(f"Container image build {status}. Build logs: {link}")
"""

_BUILDSPEC = {
    "version": "0.2",
    "phases": {
        "pre_build": {"commands": [
            'aws ecr get-login-password --region "$AWS_DEFAULT_REGION" | docker login --username AWS --password-stdin "${REPO_URI%%/*}"',
        ]},
        "build": {"commands": ['docker build -t "$REPO_URI:$IMAGE_TAG" .']},
        "post_build": {"commands": ['docker push "$REPO_URI:$IMAGE_TAG"']},
    },
}


class CloudImageBuild(Construct):
    """Exposes `repository` and `tag`; depend on `resource` before using the image."""

    def __init__(self, scope: Construct, construct_id: str, *, source_dir: str) -> None:
        super().__init__(scope, construct_id)

        source = s3_assets.Asset(self, "Source", path=source_dir,
                                 exclude=["__pycache__", "*.pyc", ".venv", ".pytest_cache"])
        self.repository = ecr.Repository(self, "Repo", removal_policy=RemovalPolicy.DESTROY, empty_on_delete=True,
                                         lifecycle_rules=[ecr.LifecycleRule(max_image_count=10)])
        self.tag = source.asset_hash

        project = codebuild.Project(
            self, "Project",
            source=codebuild.Source.s3(bucket=source.bucket, path=source.s3_object_key),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                compute_type=codebuild.ComputeType.SMALL,
                privileged=True,  # required for docker build
            ),
            environment_variables={"REPO_URI": codebuild.BuildEnvironmentVariable(value=self.repository.repository_uri)},
            build_spec=codebuild.BuildSpec.from_object(_BUILDSPEC),
            timeout=Duration.minutes(30),
        )
        source.grant_read(project)
        self.repository.grant_pull_push(project)

        common = dict(runtime=lambda_.Runtime.PYTHON_3_12, code=lambda_.Code.from_inline(_HANDLER),
                      timeout=Duration.minutes(1), log_retention=logs.RetentionDays.ONE_WEEK)
        on_event = lambda_.Function(self, "StartBuild", handler="index.on_event", **common)
        is_complete = lambda_.Function(self, "CheckBuild", handler="index.is_complete", **common)
        on_event.add_to_role_policy(iam.PolicyStatement(actions=["codebuild:StartBuild"], resources=[project.project_arn]))
        is_complete.add_to_role_policy(iam.PolicyStatement(actions=["codebuild:BatchGetBuilds"], resources=[project.project_arn]))

        provider = cr.Provider(self, "Provider", on_event_handler=on_event, is_complete_handler=is_complete,
                               query_interval=Duration.seconds(20), total_timeout=Duration.minutes(40))
        self.resource = CustomResource(self, "Build", service_token=provider.service_token,
                                       properties={"ProjectName": project.project_name, "ImageTag": self.tag})
