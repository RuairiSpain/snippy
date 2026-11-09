param environmentName string
param location string
param openAiLocation string
param tags object
param targetTenantId string
param collectorClientId string
param dataverseEnvironmentUrl string
param portalEntraClientId string
param portalAllowedTenantIds string
param openAiModelName string
param openAiModelVersion string
param keyVaultSecretsUserRoleId string

var token = toLower(uniqueString(subscription().id, environmentName))
var containers = [ 'tenants', 'scans', 'evidence', 'agents', 'dutyResults', 'documents', 'tasks', 'audit' ]

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${environmentName}'
  location: location
  tags: tags
}

resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: 'log-${token}'
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${token}'
  location: location
  kind: 'web'
  tags: tags
  properties: { Application_Type: 'web', WorkspaceResourceId: logs.id }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: 'st${token}'
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: { allowBlobPublicAccess: false, allowSharedKeyAccess: false, minimumTlsVersion: 'TLS1_2', supportsHttpsTrafficOnly: true }
}

resource blob 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = { parent: storage, name: 'default' }
resource docs 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = { parent: blob, name: 'documents' }

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: 'cosmos-${token}'
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    locations: [ { locationName: location, failoverPriority: 0 } ]
    capabilities: [ { name: 'EnableServerless' } ]
    disableLocalAuth: true
    minimalTlsVersion: 'Tls12'
  }
}

resource db 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmos
  name: 'euaiaction'
  properties: { resource: { id: 'euaiaction' } }
}

resource cosmosContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = [for c in containers: {
  parent: db
  name: c
  properties: { resource: { id: c, partitionKey: { paths: [ '/tenantId' ], kind: 'Hash' } } }
}]

// Built-in "Cosmos DB Built-in Data Contributor"
resource cosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmos
  name: guid(cosmos.id, identity.id, 'data-contributor')
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    principalId: identity.properties.principalId
    scope: cosmos.id
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-${token}'
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    publicNetworkAccess: 'Enabled'
  }
}

resource openai 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: 'oai-${token}'
  location: openAiLocation
  kind: 'OpenAI'
  tags: tags
  sku: { name: 'S0' }
  properties: { customSubDomainName: 'oai-${token}', disableLocalAuth: true, publicNetworkAccess: 'Enabled' }
}

resource openaiModel 'Microsoft.CognitiveServices/accounts/deployments@2023-05-01' = {
  parent: openai
  name: 'narrative'
  sku: { name: 'Standard', capacity: 10 }
  properties: { model: { format: 'OpenAI', name: openAiModelName, version: openAiModelVersion } }
}

var roles = {
  blobOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  queueContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  tableContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  openAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
}

resource storageRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in [ roles.blobOwner, roles.queueContributor, roles.tableContributor ]: {
  scope: storage
  name: guid(storage.id, identity.id, r)
  properties: { roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r), principalId: identity.properties.principalId, principalType: 'ServicePrincipal' }
}]

resource openAiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: openai
  name: guid(openai.id, identity.id, roles.openAiUser)
  properties: { roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAiUser), principalId: identity.properties.principalId, principalType: 'ServicePrincipal' }
}

resource vaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, identity.id, keyVaultSecretsUserRoleId)
  properties: { roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId), principalId: identity.properties.principalId, principalType: 'ServicePrincipal' }
}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'plan-${token}'
  location: location
  tags: tags
  sku: { name: 'Y1', tier: 'Dynamic' }
  kind: 'functionapp'
  properties: { reserved: true }
}

resource api 'Microsoft.Web/sites@2023-12-01' = {
  name: 'func-${token}'
  location: location
  kind: 'functionapp,linux'
  tags: union(tags, { 'azd-service-name': 'api' })
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identity.id}': {} } }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'Python|3.11'
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'AzureWebJobsStorage__clientId', value: identity.properties.clientId }
        { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'APP_ENV', value: 'prod' }
        { name: 'AUTH_DISABLED', value: 'false' }
        { name: 'USE_FIXTURES', value: 'false' }
        { name: 'TARGET_TENANT_ID', value: targetTenantId }
        { name: 'COLLECTOR_CLIENT_ID', value: collectorClientId }
        { name: 'DATAVERSE_ENVIRONMENT_URL', value: dataverseEnvironmentUrl }
        { name: 'KEY_VAULT_NAME', value: vault.name }
        { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
        { name: 'STORAGE_ACCOUNT_NAME', value: storage.name }
        { name: 'AZURE_OPENAI_ENDPOINT', value: openai.properties.endpoint }
        { name: 'AZURE_OPENAI_DEPLOYMENT', value: openaiModel.name }
        { name: 'PORTAL_ENTRA_CLIENT_ID', value: portalEntraClientId }
        { name: 'PORTAL_ALLOWED_TENANT_IDS', value: portalAllowedTenantIds }
      ]
    }
  }
}

// Easy Auth: validates the Entra token and sets X-MS-CLIENT-PRINCIPAL (the API trusts that header).
// The identity provider must be an app registration whose audience is this app; unauthenticated calls get 401.
resource apiAuth 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: api
  name: 'authsettingsV2'
  properties: {
    platform: { enabled: true }
    globalValidation: { requireAuthentication: true, unauthenticatedClientAction: 'Return401' }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: { clientId: portalEntraClientId, openIdIssuer: 'https://login.microsoftonline.com/organizations/v2.0' }
        validation: { allowedAudiences: [ 'api://${portalEntraClientId}' ] }
      }
    }
  }
}

resource portal 'Microsoft.Web/staticSites@2023-12-01' = {
  name: 'swa-${token}'
  location: location
  tags: union(tags, { 'azd-service-name': 'portal' })
  sku: { name: 'Standard', tier: 'Standard' }
  properties: {}
}

output cosmosEndpoint string = cosmos.properties.documentEndpoint
output storageName string = storage.name
output keyVaultName string = vault.name
output openAiEndpoint string = openai.properties.endpoint
