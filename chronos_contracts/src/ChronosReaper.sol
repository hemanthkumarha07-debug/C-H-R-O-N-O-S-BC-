// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IFlashLoanSimpleReceiver, IPool, IPoolAddressesProvider} from "./interfaces/IAaveV3.sol";
import {IERC20} from "./interfaces/IERC20.sol";
import {ISwapRouter} from "./interfaces/ISwapRouter.sol";
import {SafeTransferLib} from "./libraries/SafeTransferLib.sol";

/// @title ChronosReaper
/// @notice Atomic Aave V3 flash-loan arbitrage receiver executing two-leg DEX swaps.
contract ChronosReaper is IFlashLoanSimpleReceiver {
    using SafeTransferLib for address;

    struct ArbitrageParams {
        address routerBuy;
        address routerSell;
        address intermediateToken;
        uint24 feeBuy;
        uint24 feeSell;
        uint256 minIntermediateOut;
        uint256 minProfit;
    }

    IPoolAddressesProvider public immutable override ADDRESSES_PROVIDER;
    IPool public immutable override POOL;

    address public owner;
    address public pendingOwner;
    bool public paused;
    uint256 private _locked = 1;

    event StrikeInitiated(address indexed asset, uint256 amount, address indexed intermediateToken);
    event StrikeExecuted(address indexed asset, uint256 amount, uint256 premium, uint256 netProfit);
    event EmergencyWithdraw(address indexed token, address indexed recipient, uint256 amount);
    event EmergencyWithdrawETH(address indexed recipient, uint256 amount);
    event OwnershipTransferStarted(address indexed previousOwner, address indexed newOwner);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event PausedStateChanged(bool paused);

    error Unauthorized();
    error InvalidPoolCallback();
    error InvalidInitiator();
    error InvalidParameters();
    error ZeroAddress();
    error ZeroAmount();
    error ContractPaused();
    error ReentrancyGuardLocked();
    error InsufficientProfit(uint256 finalBalance, uint256 requiredAmount);
    error ETHTransferFailed();

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    modifier whenNotPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    modifier nonReentrant() {
        if (_locked != 1) revert ReentrancyGuardLocked();
        _locked = 2;
        _;
        _locked = 1;
    }

    constructor(address _addressesProvider) {
        if (_addressesProvider == address(0)) revert ZeroAddress();
        address poolAddr = IPoolAddressesProvider(_addressesProvider).getPool();
        if (poolAddr == address(0)) revert ZeroAddress();

        ADDRESSES_PROVIDER = IPoolAddressesProvider(_addressesProvider);
        POOL = IPool(poolAddr);
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    receive() external payable {}

    /// @notice Initiate an atomic flash-loan arbitrage across two Uniswap-V3-compatible routers.
    function initiateStrike(address asset, uint256 amount, ArbitrageParams calldata arbParams)
        external
        onlyOwner
        whenNotPaused
        nonReentrant
    {
        if (asset == address(0)) revert ZeroAddress();
        if (amount == 0) revert ZeroAmount();
        if (
            arbParams.routerBuy == address(0) || arbParams.routerSell == address(0)
                || arbParams.intermediateToken == address(0)
        ) {
            revert ZeroAddress();
        }
        if (arbParams.intermediateToken == asset) revert InvalidParameters();

        emit StrikeInitiated(asset, amount, arbParams.intermediateToken);

        POOL.flashLoanSimple(address(this), asset, amount, abi.encode(arbParams), 0);
    }

    /// @notice Aave V3 flashLoanSimple callback. Executes both swap legs atomically and repays POOL.
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        if (msg.sender != address(POOL)) revert InvalidPoolCallback();
        if (initiator != address(this)) revert InvalidInitiator();

        ArbitrageParams memory arb = abi.decode(params, (ArbitrageParams));

        // Leg 1: Swap borrowed asset -> intermediate token on routerBuy
        asset.forceApprove(arb.routerBuy, amount);
        uint256 intermediateOut = ISwapRouter(arb.routerBuy).exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn: asset,
                tokenOut: arb.intermediateToken,
                fee: arb.feeBuy,
                recipient: address(this),
                deadline: block.timestamp,
                amountIn: amount,
                amountOutMinimum: arb.minIntermediateOut,
                sqrtPriceLimitX96: 0
            })
        );
        asset.forceApprove(arb.routerBuy, 0);

        // Leg 2: Swap intermediate token -> borrowed asset on routerSell
        uint256 amountOwed = amount + premium;
        arb.intermediateToken.forceApprove(arb.routerSell, intermediateOut);
        ISwapRouter(arb.routerSell).exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn: arb.intermediateToken,
                tokenOut: asset,
                fee: arb.feeSell,
                recipient: address(this),
                deadline: block.timestamp,
                amountIn: intermediateOut,
                amountOutMinimum: amountOwed + arb.minProfit,
                sqrtPriceLimitX96: 0
            })
        );
        arb.intermediateToken.forceApprove(arb.routerSell, 0);

        uint256 finalBalance = IERC20(asset).balanceOf(address(this));
        uint256 requiredAmount = amountOwed + arb.minProfit;
        if (finalBalance < requiredAmount) {
            revert InsufficientProfit(finalBalance, requiredAmount);
        }

        // Approve POOL to pull principal + flash loan fee
        asset.forceApprove(address(POOL), amountOwed);

        // Skim net profit directly to owner
        uint256 profit = finalBalance - amountOwed;
        if (profit > 0) {
            asset.safeTransfer(owner, profit);
        }

        emit StrikeExecuted(asset, amount, premium, profit);
        return true;
    }

    /// @notice Pause or unpause flash-loan execution.
    function setPaused(bool _paused) external onlyOwner {
        paused = _paused;
        emit PausedStateChanged(_paused);
    }

    /// @notice Start two-step ownership transfer.
    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        pendingOwner = newOwner;
        emit OwnershipTransferStarted(owner, newOwner);
    }

    /// @notice Accept pending ownership transfer.
    function acceptOwnership() external {
        if (msg.sender != pendingOwner) revert Unauthorized();
        address previousOwner = owner;
        owner = pendingOwner;
        pendingOwner = address(0);
        emit OwnershipTransferred(previousOwner, owner);
    }

    /// @notice Extract any ERC-20 token balance held by this contract.
    function emergencyWithdraw(address tokenAddress) external onlyOwner {
        if (tokenAddress == address(0)) revert ZeroAddress();
        uint256 bal = IERC20(tokenAddress).balanceOf(address(this));
        if (bal > 0) {
            tokenAddress.safeTransfer(owner, bal);
            emit EmergencyWithdraw(tokenAddress, owner, bal);
        }
    }

    /// @notice Extract any native ETH balance held by this contract.
    function emergencyWithdrawETH() external onlyOwner {
        uint256 bal = address(this).balance;
        if (bal > 0) {
            (bool ok,) = payable(owner).call{value: bal}("");
            if (!ok) revert ETHTransferFailed();
            emit EmergencyWithdrawETH(owner, bal);
        }
    }
}
