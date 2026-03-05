# Finomics Data Pipeline

## Overview

The **Finomics Data Pipeline** is a multi-cloud cost management ETL system that ingests, processes, and stores cost data from multiple cloud providers (AWS, Azure, GCP, Alibaba, and OCI) into a unified data warehouse. This project implements a medallion architecture (Bronze, Silver, Gold layers) using AWS Glue, Lambda, Step Functions, and S3/Iceberg tables.

## Purpose

This data pipeline automates the collection and processing of cloud cost data across multiple providers to:
- Centralize cost data from AWS, Azure, GCP, Alibaba, and Oracle Cloud Infrastructure (OCI)
- Transform raw cost data through Bronze, Silver, and Gold layers
- Enable financial analytics and reporting through a unified data warehouse
- Support incremental and full load modes for cost data ingestion
- Orchestrate complex multi-cloud ETL workflows using AWS Step Functions

## Architecture

### Components

#### 1. **AWS Glue Jobs** (`src/glue/`)
ETL jobs organized by cloud provider that process cost data through the medallion architecture:
- **Bronze Layer**: Raw data ingestion from cloud provider APIs
- **Silver Layer**: Data cleansing, normalization, and enrichment
- **Gold Layer**: Aggregated and business-ready datasets

**Supported Cloud Providers:**
- `alibaba/` - Alibaba Cloud cost data jobs
- `aws/` - AWS cost data jobs
- `azure/` - Azure cost data jobs
- `gcp/` - Google Cloud Platform cost data jobs
- `oci/` - Oracle Cloud Infrastructure cost data jobs

#### 2. **AWS Lambda Functions** (`src/lambda/`)
Serverless functions for credential management and orchestration:
- `FetchAWSCreds.py` - Retrieve AWS credentials
- `FetchAlibabaCreds.py` - Retrieve Alibaba Cloud credentials
- `FetchAzureCreds.py` - Retrieve Azure credentials
- `FetchGCPCreds.py` - Retrieve GCP credentials
- `FetchOCICreds.py` - Retrieve OCI credentials
- `dev_finomics_status_update.py` - Pipeline status tracking
- `finomics-etl-trigger.py` - ETL workflow trigger

#### 3. **AWS Step Functions** (`src/step_function/`)
State machines for orchestrating multi-cloud ETL workflows:
- `GenericStepFunction.json` - Dev environment orchestration
- `StageGenericStepFunction.json` - Stage environment orchestration
- `ProdGenericStepFunction.json` - Production environment orchestration

#### 4. **S3 Scripts & Configuration** (`src/s3/scripts/`)
- `common/utils.py` - Shared utility functions
- `config/azure_config.json` - Azure-specific configurations
- `config/gcp_config.json` - GCP-specific configurations

## CI/CD Pipeline

The project uses **Azure DevOps pipelines** for continuous deployment across environments:

### Pipeline Files
- `dev-data-pipeline.yml` - Development environment pipeline
- `stage-data-pipeline.yml` - Staging environment pipeline
- `prod-data-pipeline.yml` - Production environment pipeline

### Pipeline Features

#### Change Detection
- Automatically detects changed files in Git commits
- Only deploys components that have been modified
- Supports both PR builds and direct branch pushes

### Environment Variables

Each environment pipeline requires the following Azure DevOps variable group:
- `{env}-data-pipeline-aws` containing:
  - `AWS_ACCESS_KEY_ID` - AWS access key
  - `AWS_SECRET_ACCESS_KEY` - AWS secret key
  - `AWS_ACCOUNT_ID` - AWS account ID
  - `GLUE_ROLE_ARN` - IAM role for Glue jobs
  - `S3_BUCKET` - Target S3 bucket for scripts and data

## Project Structure

```
finomics_glue_data_pipeline/
├── src/
│   ├── glue/              # AWS Glue ETL jobs
│   │   ├── alibaba/       # Alibaba Cloud jobs
│   │   ├── aws/           # AWS jobs
│   │   ├── azure/         # Azure jobs
│   │   ├── gcp/           # GCP jobs
│   │   └── oci/           # OCI jobs
│   ├── lambda/            # Lambda functions
│   ├── s3/                # S3 scripts and configs
│   │   └── scripts/
│   │       ├── common/    # Shared utilities
│   │       └── config/    # Cloud provider configs
│   └── step_function/     # Step Function definitions
├── dev-data-pipeline.yml  # Dev CI/CD pipeline
├── stage-data-pipeline.yml # Stage CI/CD pipeline
├── prod-data-pipeline.yml # Prod CI/CD pipeline
└── README.md
```

## Data Flow

1. **Orchestration**: AWS Step Functions trigger the pipeline based on client requests
2. **Credential Retrieval**: Lambda functions fetch cloud provider credentials
3. **Bronze Layer**: Glue jobs ingest raw cost data from cloud provider APIs
4. **Silver Layer**: Data is cleansed, normalized, and enriched
5. **Gold Layer**: Aggregated datasets are created for analytics
6. **Storage**: All data is stored in S3 using Iceberg table format

## Load Modes

The pipeline supports two load modes:
- **Full Load**: Complete historical data ingestion
- **Incremental Load**: Delta updates for new/modified cost data

## Technologies

- **AWS Services**: Glue, Lambda, Step Functions, S3, IAM
- **Data Format**: Apache Iceberg
- **Language**: Python 3.x
- **Orchestration**: AWS Step Functions
- **CI/CD**: Azure DevOps Pipelines
- **Version Control**: Azure DevOps Git

## Getting Started

### Prerequisites
- AWS Account with appropriate IAM permissions
- Azure DevOps account and project
- Python 3.x installed locally for development
- AWS CLI configured


## Branch Strategy

- `dev` - Development environment (auto-deploys to dev AWS resources)
- `stage` - Staging environment (auto-deploys to stage AWS resources)
- `prod` - Production environment (auto-deploys to prod AWS resources)

## Contributing

1. Create a feature branch from `dev`
2. Make your changes
3. Test thoroughly in dev environment
4. Create a pull request to `dev`
5. After approval, changes will be promoted to `stage` and `prod`

## Support

For issues or questions, please contact the Finomics data engineering team or create an issue in the Azure DevOps repository.