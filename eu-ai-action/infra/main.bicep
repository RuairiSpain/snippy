// NOT VALIDATED: no Bicep or Azure CLI was available when this was written. Run `az bicep build` and
// `azd provision --preview` before the first deployment.
targetScope = 'subscription'

@minLength(1) @maxLength(32)
param environmentName string
param location string
@description('Region for Azure OpenAI; must offer the chosen model in an EU region for EU data residency.')
param openAiLocation string
param targetTenantId string
param collectorClientId string
param dataverseEnvironmentUrl string
param portalEntraClientId string
param portalAllowedTenantIds string = ''
param openAiModelName string = 'gpt-4o'
param openAiModelVersion string = '2024-11-20'
@description('GUID of the built-in role "Key Vault Secrets User". Look it up with: az role definition list --name "Key Vault Secrets User" --query [0].name -o tsv')
param keyVaultSecretsUserRoleId string

var tags = { 'azd-env-name': environmentName, project: 'eu-ai-action' }

resource rg 'Microsoft.Resources/resourceGroups@2022-09-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    environmentName: environmentName
    location: location
    openAiLocation: openAiLocation
    tags: tags
    targetTenantId: targetTenantId
    collectorClientId: collectorClientId
    dataverseEnvironmentUrl: dataverseEnvironmentUrl
    portalEntraClientId: portalEntraClientId
    portalAllowedTenantIds: portalAllowedTenantIds
    openAiModelName: openAiModelName
    openAiModelVersion: openAiModelVersion
    keyVaultSecretsUserRoleId: keyVaultSecretsUserRoleId
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output COSMOS_ENDPOINT string = resources.outputs.cosmosEndpoint
output STORAGE_ACCOUNT_NAME string = resources.outputs.storageName
output KEY_VAULT_NAME string = resources.outputs.keyVaultName
output AZURE_OPENAI_ENDPOINT string = resources.outputs.openAiEndpoint
