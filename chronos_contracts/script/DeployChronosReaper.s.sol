// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Script} from "forge-std/Script.sol";
import {ChronosReaper} from "../src/ChronosReaper.sol";

contract DeployChronosReaper is Script {
    // Ethereum Mainnet Aave V3 PoolAddressesProvider
    address internal constant DEFAULT_AAVE_ADDRESSES_PROVIDER = 0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e;

    function run() public returns (ChronosReaper reaper) {
        address provider = vm.envOr("AAVE_ADDRESSES_PROVIDER", DEFAULT_AAVE_ADDRESSES_PROVIDER);

        vm.startBroadcast();
        reaper = new ChronosReaper(provider);
        vm.stopBroadcast();
    }
}
