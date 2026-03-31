import os
import sys
import subprocess

# Step 1: Update the system and install necessary packages
subprocess.check_call(["apt-get", "update"])
subprocess.check_call(["apt-get", "install", "-y", "git", "python3-pip", "python3-dev", "libxml2-dev", "libxslt1-dev"])

# Step 2: Install pip requirements
requirements = [
    "wheel",
    "setuptools",
]

for req in requirements:
    subprocess.check_call(["pip3", "install", req])

# Step 3: Clone the Odoo repository
subprocess.check_call(["git", "clone", "https://github.com/odoo/odoo.git"])

# Step 4: Install Odoo requirements
subprocess.check_call(["pip3", "install", "-r", "odoo/requirements.txt"])

# Step 5: Start the Odoo server
os.chdir("odoo")
subprocess.check_call(["python3", "odoo-bin", "-c", "odoo.conf"])
