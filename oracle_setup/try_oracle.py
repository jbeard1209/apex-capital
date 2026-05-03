#!/usr/bin/env python3
# =============================================================================
# ORACLE CLOUD — AUTO INSTANCE CREATOR
# Tries to create a free Ampere VM every 5 minutes across all availability
# domains until it succeeds, then sends an email to the Chairman.
# =============================================================================

import oci
import time
import smtplib
import os
import sys
from email.mime.text import MIMEText
from datetime import datetime

# ── GitHub Personal Access Token (for auto-deployment) ───────────────────────
# Generate at: github.com → Settings → Developer settings → Personal access tokens
# Needs "repo" scope (read access to clone private repo)
GITHUB_TOKEN = "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"

# ── Oracle credentials ────────────────────────────────────────────────────────
OCI_USER        = "ocid1.user.oc1..aaaaaaaa5pv7h7uldnd6r4t77qqczij5jm7fiyn6cgz7nuutv4xor3c7pfda"
OCI_FINGERPRINT = "5a:5f:4e:1a:fe:05:2c:42:fc:aa:0b:4b:94:26:a2:30"
OCI_TENANCY     = "ocid1.tenancy.oc1..aaaaaaaaffjqiqmjtws444dk2ke7swmddkdsrdwe6mhqkmowkul7tb5zcjea"
OCI_REGION      = "us-ashburn-1"
OCI_KEY_FILE    = os.path.join(os.path.dirname(__file__), "oci_key.pem")

# ── Instance config ───────────────────────────────────────────────────────────
COMPARTMENT_ID  = OCI_TENANCY   # using root compartment
SHAPE           = "VM.Standard.A1.Flex"
IMAGE_NAME      = "Canonical Ubuntu 22.04"
OCPUS           = 1
MEMORY_GB       = 6
INSTANCE_NAME   = "apex-capital-server"
RETRY_INTERVAL  = 300           # 5 minutes between attempts

# Your SSH public key
SSH_PUBLIC_KEY  = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINqOSnrPE9sQpT0eKtnNJOhmzXMWYv6Q8gXcpRmRYKBS jack.beard07@gmail.com"

# Email settings
EMAIL_SENDER    = "jack.beard07@gmail.com"
EMAIL_RECIPIENT = "jack.beard07@gmail.com"
GMAIL_PASSWORD  = "vaje mgnh jhtm kknf"


def send_success_email(instance, public_ip):
    """Email the Chairman when the instance is created."""
    subject = "✅ Oracle Cloud Server Created — Apex Capital is ready to deploy"
    body = f"""
Your Oracle Cloud free server has been successfully created!

Instance Name:  {instance.display_name}
Instance ID:    {instance.id}
Public IP:      {public_ip}
Shape:          {SHAPE}
Created at:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Next step — SSH into your server from Terminal:
    ssh ubuntu@{public_ip}

Then reply to this email or tell Claude the public IP and we'll deploy the trading system.
"""
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, GMAIL_PASSWORD)
            server.send_message(msg)
        print("  ✅ Success email sent to your Gmail!")
    except Exception as e:
        print(f"  ⚠ Email failed (but instance was created!): {e}")


def get_ubuntu_image_id(compute_client, compartment_id):
    """Find the latest Ubuntu 22.04 ARM image."""
    images = compute_client.list_images(
        compartment_id,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        shape=SHAPE,
    ).data
    if not images:
        # Try without shape filter
        images = compute_client.list_images(
            compartment_id,
            operating_system="Canonical Ubuntu",
            operating_system_version="22.04",
        ).data
    if images:
        return images[0].id
    raise Exception("Could not find Ubuntu 22.04 image")


def get_availability_domains(identity_client, compartment_id):
    """Get all availability domains in the region."""
    ads = identity_client.list_availability_domains(compartment_id).data
    return [ad.name for ad in ads]


def get_or_create_subnet(network_client, compartment_id):
    """Get an existing subnet or create a VCN + subnet if none exist."""
    # Check for existing VCN
    vcns = network_client.list_vcns(compartment_id).data
    if vcns:
        vcn_id = vcns[0].id
        subnets = network_client.list_subnets(compartment_id, vcn_id=vcn_id).data
        if subnets:
            return subnets[0].id
    else:
        # Create a new VCN
        print("  No VCN found — creating one...")
        vcn = network_client.create_vcn(
            oci.core.models.CreateVcnDetails(
                cidr_block     = "10.0.0.0/16",
                compartment_id = compartment_id,
                display_name   = "apex-capital-vcn",
                dns_label      = "apexvcn",
            )
        ).data
        vcn_id = vcn.id

        # Create internet gateway
        ig = network_client.create_internet_gateway(
            oci.core.models.CreateInternetGatewayDetails(
                compartment_id = compartment_id,
                vcn_id         = vcn_id,
                is_enabled     = True,
                display_name   = "apex-capital-ig",
            )
        ).data

        # Update default route table to point to internet gateway
        rt_id = network_client.get_vcn(vcn_id).data.default_route_table_id
        network_client.update_route_table(
            rt_id,
            oci.core.models.UpdateRouteTableDetails(
                route_rules=[
                    oci.core.models.RouteRule(
                        destination       = "0.0.0.0/0",
                        network_entity_id = ig.id,
                    )
                ]
            )
        )
        print(f"  ✓ VCN created: {vcn_id[:40]}...")

    # Create subnet
    print("  Creating public subnet...")
    subnet = network_client.create_subnet(
        oci.core.models.CreateSubnetDetails(
            cidr_block        = "10.0.0.0/24",
            compartment_id    = compartment_id,
            vcn_id            = vcn_id,
            display_name      = "apex-capital-subnet",
            dns_label         = "apexsubnet",
            prohibit_public_ip_on_vnic = False,
        )
    ).data
    print(f"  ✓ Subnet created: {subnet.id[:40]}...")
    return subnet.id


def try_create_instance(compute_client, image_id, subnet_id, availability_domain):
    """Attempt to create an instance in the given availability domain."""
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain = availability_domain,
        compartment_id      = COMPARTMENT_ID,
        display_name        = INSTANCE_NAME,
        shape               = SHAPE,
        shape_config        = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus        = OCPUS,
            memory_in_gbs= MEMORY_GB,
        ),
        source_details = oci.core.models.InstanceSourceViaImageDetails(
            image_id    = image_id,
            source_type = "image",
        ),
        create_vnic_details = oci.core.models.CreateVnicDetails(
            subnet_id               = subnet_id,
            assign_public_ip        = True,
            assign_private_dns_record = True,
        ),
        metadata = {
            "ssh_authorized_keys": SSH_PUBLIC_KEY,
        },
    )
    return compute_client.launch_instance(details).data


def get_public_ip(compute_client, network_client, instance_id, compartment_id):
    """Wait for and retrieve the public IP of the instance."""
    for _ in range(30):
        time.sleep(10)
        vnic_attachments = compute_client.list_vnic_attachments(
            compartment_id, instance_id=instance_id
        ).data
        if vnic_attachments:
            vnic = network_client.get_vnic(vnic_attachments[0].vnic_id).data
            if vnic.public_ip:
                return vnic.public_ip
    return "unknown (check Oracle console)"


def main():
    print("=" * 60)
    print("  APEX CAPITAL — Oracle Cloud Auto Instance Creator")
    print("=" * 60)
    print(f"  Trying every {RETRY_INTERVAL//60} minutes until capacity is available.")
    print(f"  You'll get an email at {EMAIL_RECIPIENT} when it succeeds.")
    print(f"  Press Ctrl+C to stop.\n")

    # Set up OCI clients
    config = {
        "user":        OCI_USER,
        "fingerprint": OCI_FINGERPRINT,
        "tenancy":     OCI_TENANCY,
        "region":      OCI_REGION,
        "key_file":    OCI_KEY_FILE,
    }

    try:
        compute_client  = oci.core.ComputeClient(config)
        network_client  = oci.core.VirtualNetworkClient(config)
        identity_client = oci.identity.IdentityClient(config)
    except Exception as e:
        print(f"  ❌ Failed to connect to Oracle API: {e}")
        sys.exit(1)

    # Get image ID and subnet
    try:
        print("  Looking up Ubuntu 22.04 image...")
        image_id  = get_ubuntu_image_id(compute_client, COMPARTMENT_ID)
        print(f"  ✓ Image found: {image_id[:40]}...")

        print("  Looking up subnet (will create if needed)...")
        subnet_id = get_or_create_subnet(network_client, COMPARTMENT_ID)
        if not subnet_id:
            print("  ⚠ No existing subnet found — Oracle will create one automatically.")

        print("  Looking up availability domains...")
        ads = get_availability_domains(identity_client, COMPARTMENT_ID)
        print(f"  ✓ Found {len(ads)} availability domain(s): {', '.join(ads)}\n")
    except Exception as e:
        print(f"  ❌ Setup error: {e}")
        sys.exit(1)

    attempt = 0
    while True:
        attempt += 1
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"  [{timestamp}] Attempt #{attempt} — trying {len(ads)} availability domain(s)...")

        for ad in ads:
            try:
                print(f"    → Trying {ad}...", end=" ", flush=True)
                instance = try_create_instance(
                    compute_client, image_id, subnet_id, ad
                )
                print("✅ SUCCESS!")
                print(f"\n  🎉 Instance created in {ad}!")
                print(f"  Instance ID: {instance.id}")
                print(f"  Waiting for public IP...")

                public_ip = get_public_ip(
                    compute_client, network_client, instance.id, COMPARTMENT_ID
                )
                print(f"  Public IP: {public_ip}")
                send_success_email(instance, public_ip)

                # ── Automatically deploy the trading system ───────────────
                if GITHUB_TOKEN and GITHUB_TOKEN != "GITHUB_TOKEN_PLACEHOLDER":
                    print("\n  🚀 Starting automated deployment...")
                    try:
                        import subprocess
                        deploy_script = os.path.join(
                            os.path.dirname(__file__), "deploy.py"
                        )
                        subprocess.run(
                            [sys.executable, deploy_script, public_ip, GITHUB_TOKEN],
                            check=True
                        )
                    except Exception as deploy_err:
                        print(f"  ⚠ Auto-deploy failed: {deploy_err}")
                        print(f"  Server IP is {public_ip} — tell Claude to deploy manually.")
                else:
                    print(f"\n  ⚠ No GitHub token set — skipping auto-deploy.")
                    print(f"  Server IP: {public_ip}")
                    print(f"  Tell Claude the IP to complete setup manually.")

                print("\n  Done! Check your email for next steps.")
                sys.exit(0)

            except oci.exceptions.ServiceError as e:
                if "Out of capacity" in str(e) or "InternalError" in str(e):
                    print("out of capacity")
                else:
                    print(f"error: {e.message}")
            except Exception as e:
                print(f"error: {e}")

        next_try = datetime.now().strftime("%H:%M:%S")
        print(f"  All domains full. Waiting {RETRY_INTERVAL//60} min before next attempt...")
        print(f"  (Next try at approximately {next_try})\n")
        time.sleep(RETRY_INTERVAL)


if __name__ == "__main__":
    main()
