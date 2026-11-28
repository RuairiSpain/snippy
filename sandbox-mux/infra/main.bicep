// Sandbox group for sandbox-mux. Resource shapes follow the Container Apps Sandboxes
// Bicep quickstart (API version 2026-02-01-preview). The preview API can change.

@description('Region for all resources. Pick a region where Container Apps Sandboxes is available.')
param location string = resourceGroup().location

@description('Sandbox group name.')
param sandboxGroupName string = 'sandbox-mux'

@description('Upper bound on sandboxes in the group. Keep it at or above pool.max_sandboxes in config.yaml.')
param maxSandboxCount int = 10

@description('Object ID of the identity the session manager runs as. It receives the data-plane role.')
param orchestratorPrincipalId string

@allowed(['ServicePrincipal', 'User', 'Group'])
param orchestratorPrincipalType string = 'ServicePrincipal'

@description('Set true to attach the group to a new VNet. The subnet cannot be changed afterwards.')
param useVnet bool = false

param vnetAddressPrefix string = '10.0.0.0/16'
param subnetAddressPrefix string = '10.0.0.0/23'

// Container Apps SandboxGroup Data Owner
var dataOwnerRoleId = 'c24cf47c-5077-412d-a19c-45202126392c'

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = if (useVnet) {
  name: '${sandboxGroupName}-vnet'
  location: location
  properties: {
    addressSpace: { addressPrefixes: [vnetAddressPrefix] }
    subnets: [
      {
        name: 'sandbox-subnet'
        properties: {
          addressPrefix: subnetAddressPrefix
          delegations: [
            {
              name: 'sandboxDelegation'
              properties: { serviceName: 'Microsoft.App/environments' }
            }
          ]
        }
      }
    ]
  }
}

resource sandboxGroup 'Microsoft.App/sandboxGroups@2026-02-01-preview' = {
  name: sandboxGroupName
  location: location
  properties: {
    maxSandboxCount: maxSandboxCount
  }
}

resource vnetConnection 'Microsoft.App/sandboxGroups/vnetConnections@2026-02-01-preview' = if (useVnet) {
  parent: sandboxGroup
  name: 'default'
  location: location
  properties: {
    subnetId: vnet.properties.subnets[0].id
  }
}

resource dataOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sandboxGroup.id, orchestratorPrincipalId, dataOwnerRoleId)
  scope: sandboxGroup
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', dataOwnerRoleId)
    principalId: orchestratorPrincipalId
    principalType: orchestratorPrincipalType
  }
}

output sandboxGroupName string = sandboxGroup.name
output sandboxGroupId string = sandboxGroup.id
