# C.H.R.O.N.O.S. Smart Contracts

Foundry workspace for [`ChronosReaper.sol`](src/ChronosReaper.sol), an Aave V3 `flashLoanSimple` arbitrage receiver implementing two-leg Uniswap-V3-compatible swaps, strict callback verification, safe ERC-20 handling, circuit-breaker pausing, and two-step ownership.

## Prerequisites

Install [Foundry](https://book.getfoundry.sh/getting-started/installation) (`forge`, `cast`, `anvil`).

## Build, Format & Test

```shell
forge fmt --check
forge build --sizes
forge test -vvv
```

## Deploy (Sepolia / Mainnet Fork)

```shell
forge script script/DeployChronosReaper.s.sol:DeployChronosReaper \
  --rpc-url "$ALCHEMY_RPC_URL" \
  --private-key "$DEPLOYER_PRIVATE_KEY" \
  --broadcast
```
