// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "../interfaces/IERC20.sol";

/// @title SafeTransferLib
/// @notice Minimal ERC-20 wrapper handling tokens that omit a boolean return or disallow non-zero approve overwrites.
library SafeTransferLib {
    error SafeTransferFailed();
    error SafeApproveFailed();

    function safeTransfer(address token, address to, uint256 amount) internal {
        (bool ok, bytes memory data) = token.call(abi.encodeCall(IERC20.transfer, (to, amount)));
        if (!ok || (data.length != 0 && !abi.decode(data, (bool)))) {
            revert SafeTransferFailed();
        }
    }

    function forceApprove(address token, address spender, uint256 amount) internal {
        (bool ok, bytes memory data) = token.call(abi.encodeCall(IERC20.approve, (spender, amount)));
        if (ok && (data.length == 0 || abi.decode(data, (bool)))) {
            return;
        }

        // Reset to zero first for tokens like USDT that revert on non-zero -> non-zero approve
        (bool resetOk, bytes memory resetData) = token.call(abi.encodeCall(IERC20.approve, (spender, 0)));
        if (!resetOk || (resetData.length != 0 && !abi.decode(resetData, (bool)))) {
            revert SafeApproveFailed();
        }

        (bool retryOk, bytes memory retryData) = token.call(abi.encodeCall(IERC20.approve, (spender, amount)));
        if (!retryOk || (retryData.length != 0 && !abi.decode(retryData, (bool)))) {
            revert SafeApproveFailed();
        }
    }
}
